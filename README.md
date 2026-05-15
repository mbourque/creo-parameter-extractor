# Creo Parameter Extractor

## What this app does

This is a small **Windows desktop tool** that reads **Creo Parametric part parameters** from many models in one go and writes them to a **single text report**.

You choose a folder of `.prt` files (including numbered backups like `part.prt.7`). The app connects to **CREOSON**, which in turn controls your **already-running** Creo session. For each part it opens the model in Creo (in the background), asks Creo for **all parameter names and values** (for example Part Number, Description, Manufacturer), then writes one section per file to your output path.

You do **not** open each part by hand or copy parameters from the Creo UI. You do **not** need to write JSON or code to use the GUI—only to run Creo, run CREOSON, pick folder and output file, and click **Extract parameters**.

**Good for:** BOM prep, audits, comparing parameter data across a library of parts, or exporting metadata to share outside Creo.

**Not for:** editing parameters in Creo, building assemblies, or replacing PTC’s own tools—this tool **reads** parameters only.

## Using the UI

### Fields


| Field             | Purpose                                                                                                                     |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------- |
| **Models folder** | Directory containing your `.prt` files (and/or numbered backups like `part.prt.7`). Use **Browse…** to pick a folder.       |
| **CREOSON host**  | Usually `localhost` if CREOSON runs on the same PC. Use another hostname or IP only if CREOSON runs on a different machine. |
| **Port**          | CREOSON port (default `9056`). Must match your CREOSON setup.                                                               |
| **Output file**   | Path for the text report (e.g. `C:\reports\parameters.txt`). Use **Browse…** to choose.                                     |


### Run extraction

1. Start **Creo Parametric** and wait until it is fully loaded (no blocking dialogs).
2. Start **CREOSON** and confirm it is listening on the port you entered.
3. Run this app — either double-click `**CreoPRTParameterExtractor.exe`** in the project folder, or from a terminal run `python prt_parameter_extractor.py` (Python install required for the latter; see **Installation and setup** below).
4. Set **Models folder** and **Output file**.
5. Click **Extract parameters**.
6. Watch the **Log** area for progress. When finished, a dialog confirms success or lists errors.

### Typical workflow

```
Creo running → CREOSON running → Open this app → Choose folder & output → Extract
```

**Shutdown (when you are done):** close this app, stop CREOSON, then exit Creo. If **Stop CREOSON** hangs, exit Creo first, then stop CREOSON or end its process in Task Manager.

The log should show `Checking TCP localhost:9056 …` then `Connecting to CREOSON …` without a long timeout. A ~60s timeout usually means Creo was not running or CREOSON was not linked to it.

## Which `.prt` files are processed?

For each logical part name in the models folder:

- If `**name.prt`** exists, that file is used.
- Otherwise the **highest numbered** backup is used (e.g. `name.prt.10` over `name.prt.9`).

Files that are only `*.prt.N` on disk (e.g. `ec-j1000-0011.prt.7`) are opened by copying a temporary `*.prt` **in the same folder** (Creo cannot open `.prt.N` by name through CREOSON). The report still labels the section with the **original disk filename**.

Models are opened in **non-display** mode for stable batch automation (no on-screen flash per part).

## Output file format

Each model gets a header block, then all parameters (sorted by name):

```text
========================================================================
MODEL FILE (disk): ec-j1000-0011.prt.7
Creo session name: ec-j1000-0011.__cextmp_a1b2c3d4.prt
========================================================================

Part Number: EC-J1000-0011
Description: 0.635mm ERM6 EDGE RATE 2X30
Manufacturer: Samtec
Manufacturer_PN: ERM6-30-01.5-L-DV-A-K
```

Multiple models in one folder are written to the **same output file**, with sections separated by a line of `=` characters.

If some files fail, the report still contains successful sections and an **Errors** section at the end.

## Troubleshooting


| Symptom                                                 | What to try                                                                                                                      |
| ------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| Hangs at “Connecting to CREOSON…” then times out (~60s) | Start Creo first, then CREOSON. Dismiss any Creo modal dialogs.                                                                  |
| `Unknown Model Extension`                               | Usually an old issue with opening `.prt.N` directly; use the current version of this tool.                                       |
| `Pro/TOOLKIT … General Error`                           | Open the part manually in Creo; fix regen errors or missing references. Ensure related files sit in the same folder as the part. |
| CREOSON **Stop** hangs                                  | Exit Creo first, then stop CREOSON; or end the CREOSON/Java process in Task Manager.                                             |
| `0 .prt file(s) found`                                  | Folder path wrong, or no files matching `*.prt` / `*.prt.N` in that directory.                                                   |


---

## Installation and setup

### What you need


| Requirement         | Notes                                                                                   |
| ------------------- | --------------------------------------------------------------------------------------- |
| **Creo Parametric** | Must be **running** before you extract. CREOSON drives Creo through J-Link.             |
| **CREOSON**         | Micro-server listening on a port (default **9056**). Start CREOSON after Creo is up.    |
| **J-Link**          | Included with Creo (automation interface); may need to be enabled per your PTC install. |
| **Python 3.10+**    | Required to run this application (see below).                                           |


Starting only the CREOSON Java server without a live Creo session will cause connection timeouts or failed opens.

### Install this application (Python)

1. Open a terminal in this project folder.
2. (Recommended) Create and activate a virtual environment:
  ```bat
   python -m venv venv
   venv\Scripts\activate
  ```
3. Install dependencies:
  ```bat
   python -m pip install -r requirements.txt
  ```
4. Run the application (pick one):
  **Executable** (no Python needed on that PC):
   **From source** (after step 3 above):

### CREOSON: where to get it and install

CREOSON is a small **JSON server** between this app and **Creo Parametric**. It translates JSON commands (open file, list parameters, etc.) into **J-Link** calls inside Creo. This tool talks to CREOSON over HTTP on the port you set (default **9056**); it does not replace CREOSON.

#### Download


| Source                     | URL                                                                                                |
| -------------------------- | -------------------------------------------------------------------------------------------------- |
| **Releases (recommended)** | [github.com/SimplifiedLogic/creoson/releases](https://github.com/SimplifiedLogic/creoson/releases) |
| **Source / docs**          | [github.com/SimplifiedLogic/creoson](https://github.com/SimplifiedLogic/creoson)                   |
| **API reference**          | [creoson.com/functions.html](https://creoson.com/functions.html)                                   |


For most users, download `**CreosonServerWithSetup-*.zip`** (setup GUI, includes Java). Choose **32-bit** or **64-bit** to match your Creo install.

#### Install CREOSON (pre-packaged)

CREOSON is not a traditional installer—you unzip and run from a folder:

1. Download `**CreosonServerWithSetup-*.zip`** from [Releases](https://github.com/SimplifiedLogic/creoson/releases).
2. Copy the ZIP to a permanent location (e.g. `C:\Tools\creoson\`) and **extract** it.
3. Run `**CreosonSetup.exe`**.
4. Set **Creo installation directory** (folder containing `parametric.exe`).
5. Set **Port** (often **9056**—use the same value in this app’s **Port** field).
6. Click **Start CREOSON** and leave it running while you extract parameters.
7. Optional: **Open Documentation** → **Playground** to test commands.

Allow the CREOSON port through Windows firewall on localhost if prompted.

#### How this app talks to CREOSON

Behind the GUI, requests include `connection : connect`, `creo : cd`, `file : open`, `parameter : list`, `file : erase`, and `connection : disconnect` (via the [creopyson](https://github.com/Zepmanbc/creopyson) library). You do not send that JSON yourself.


| Component        | Role                                                              |
| ---------------- | ----------------------------------------------------------------- |
| **CREOSON**      | Third-party server from Simplified Logic / GitHub releases.       |
| **This project** | Desktop UI and batch logic; Python deps: `creopyson`, `requests`. |


## References

- [CREOSON on GitHub](https://github.com/SimplifiedLogic/creoson)
- [CREOSON JSON API reference](https://creoson.com/functions.html)
- [creopyson](https://github.com/Zepmanbc/creopyson) (Python client used by this project)

## License

Use and modify as needed for your environment. CREOSON is MIT-licensed; Creo Parametric is a PTC product.