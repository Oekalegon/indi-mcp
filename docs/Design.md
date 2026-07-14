# **INDI MCP Server** Design

This python application exposes INDI messages using MCP to clients that can process MCP requests. This, obviously, includes LLM AI applications but can also include other types of applications that understand the MCP protocol. 

One consideration in the creation of the MCP server is that we would like to run longrunning tasks on the device that is running the INDI server and drivers, that is the device connected to USB to the different astrophotography instruments (camera's, filter wheels, etc...). The MCP server will include more functionality than just forwarding INDI messages, but will able to run sequences or scripts of INDI commands. One obvious usecase is, capturing a sequence of images/frames. The frames will be temporarily stored on the connected device. This prevents issues when the controlling computer, that is for instance, connected via WiFi, looses the connection to the INDI device. The capture sequence will continue because that is running on the INDI device and captured frames will be also be stored on the INDI device. The controlling computer can then retrieve the files when it is connected again.

## Architecture overview

Three tiers are involved: the **Client Computer** (wherever the MCP client runs), the **INDI Device** (the Raspberry Pi, or equivalent, connected to the gear), and the **Astrophotography Instruments** themselves. Within the INDI Device, the MCP Server sits above the INDI Server (`indiserver`), which in turn manages the INDI Drivers that talk to the hardware over USB/serial.

![Architecture diagram showing the Client Computer, INDI Device (with MCP Server, INDI Server and INDI Drivers layers) and the Astrophotography Instruments](images/architecture.svg)

The MCP server will need to be connected to an INDI server, which in turn will be connected to drivers and devices. Several different layers are individually exposed. These (will) include:

* INDI server layer
	* Start an INDI server (with all the possible properties liker port)
	* Stop an INDI server
	* Restart an INDI server
	* Start an INDI driver
	* Stop an INDI driver
* INDI messaging layer - Including a stream of messages being recieved from the INDI server. This will include all INDI message types (definition, new, set, message). The user can also send messages to the INDI server through mcp, thereby the user will be able to control INDI devices. This will be the most basic control layer.
* INDI scripting layer - The MCP server will include scripts for e.g. capturing a frame, capturing a sequence of frames, slewing, etc., that run INDI messages sequentially, with later messages depending on the output of earlier ones. These scripts will be defined in YAML, parsed with a safe loader (`yaml.safe_load`, never the unsafe `yaml.load`) and executed against a fixed, schema-validated set of step primitives rather than an embedded expression language. Because a script is then just declarative data, not executable code, it can safely be authored on the controlling computer and uploaded to the MCP server to run.

## MCP message format

Two different things are meant by "the JSON format" here, and only one of them is ours to design:

* The MCP **envelope** — the JSON-RPC 2.0 request/response shape, `tools/list`, `tools/call`, resources, notifications, etc. — is fully specified by the MCP protocol and implemented by the official Python MCP SDK. This project does not define or customise that layer.
* The **payload** carried inside that envelope — how an INDI property definition, update or command is represented as JSON — is entirely up to us, and is what this section defines.

INDI's own XML wire protocol encodes both the *action* (define / set / command a property) and the *data type* (Text / Number / Switch / Light / BLOB) into a single element name: `defNumberVector`, `setSwitchVector`, `newTextVector`, `delProperty`, `message`, and so on. For the MCP-facing JSON we deliberately avoid mirroring that naming and instead split it into two explicit, descriptive fields:

* `kind` — what is happening: `propertyDefinition` (INDI `def*Vector`), `propertyUpdate` (INDI `set*Vector`), `propertyCommand` (INDI `new*Vector`, client → server), `propertyDeleted` (INDI `delProperty`), or `message` (INDI `message`).
* `type` — the underlying property type: `text`, `number`, `switch`, `light`, or `blob` (INDI's `Text`/`Number`/`Switch`/`Light`/`BLOB` vectors).

For example, an INDI `defNumberVector` becomes:

```json
{
  "kind": "propertyDefinition",
  "type": "number",
  "device": "Telescope Simulator",
  "name": "EQUATORIAL_EOD_COORD",
  "label": "Eq. Coordinates",
  "state": "Ok",
  "perm": "rw",
  "elements": [
    { "name": "RA", "label": "RA (hh:mm:ss)", "value": 0.0 },
    { "name": "DEC", "label": "DEC (dd:mm:ss)", "value": 0.0 }
  ]
}
```

and a client sending an INDI `newNumberVector` to slew becomes a `propertyCommand` with the same `type`/`elements` shape. The exact schema (naming of nested fields, how BLOBs are represented/streamed, etc.) still needs to be worked out in full — this section fixes the naming convention it should follow, not the final schema.