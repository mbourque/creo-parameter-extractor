# Creo Parameter Extractor

## What this app does

This is a small **Windows desktop tool** that reads **Creo Parametric part parameters** from many models in one go and writes them to a **single HTML report**.

You choose a folder of `.prt` files (including numbered models like `part.prt.7`). The app connects to **CREOSON**, which in turn controls your **already-running** Creo session. For each part it opens the model in Creo (in the background), reads the parameters you asked for (or **all** parameters if you leave the list blank), then writes one **self-contained** `.html` file you can open in a browser.

You do **not** open each part by hand or copy parameters from the Creo UI. You do **not** need to write JSON or code to use the GUI—only to run Creo, run CREOSON, pick folder and output file, and click **Extract parameters**.

**Good for:** BOM prep, audits, comparing parameter data across a library of parts, or exporting metadata to share outside Creo.

**Not for:** editing parameters in Creo, building assemblies, or replacing PTC’s own tools—this tool **reads** parameters only.

## Using the UI

### Fields

| Field             | Purpose                                                                                                                                 |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| **Models folder** | Directory containing your `.prt` files (and/or numbered models like `part.prt.7`). Use **Browse…** to pick a folder.                     |
| **CREOSON host**  | Usually `localhost` if CREOSON runs on the same PC. Use another hostname or IP only if CREOSON runs on a different machine.               |
| **Port**          | CREOSON port (default `9056`). Must match your CREOSON setup.                                                                             |
| **Parameters**    | Comma-separated parameter names to extract (e.g. `DESCRIPTION, MATERIAL, REV`). Spaces after commas are ignored. Matching is **case-insensitive**. **Leave blank** to include **every** parameter found on any processed model (columns are the union of names across parts). |
| **Output file**   | Path for the HTML report (e.g. `C:\reports\parameters.html`). Use **Browse…** to choose a **local** folder when models live on a network share. Each part is copied there as a short-lived temp `*.prt` for Creo/CREOSON, then deleted. If you omit `.html`, it is added when you run. |

### Options menu

| Menu item                    | Purpose                                                                                                                                 |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| **Recursively find files** | **Off by default.** When enabled, searches the models folder and **all subfolders** for `.prt` / `.prt.N` files. The same filename in different folders is treated as separate parts. Saved as `recursive_search` in settings. |

### Run extraction

1. Start **Creo Parametric** and wait until it is fully loaded (no blocking dialogs).
2. Start **CREOSON** and confirm it is listening on the port you entered.
3. Run this app — either double-click `CreoParameterExtractor.exe` in the project folder, or from a terminal run `python prt_parameter_extractor.py` (Python install required for the latter; see **Installation and setup** below).
4. Set **Models folder**, **Parameters** (or leave blank for all), and **Output file**. Use **Options → Recursively find files** if parts live in subfolders.
5. Click **Extract parameters**. Use **Stop** to cancel: the current part is finished cleanly (erase from session, temp file removed), CREOSON disconnects, and a **partial** HTML report is saved if any parts completed.
6. Watch the **Log** area for progress. When finished, a dialog shows the saved path and offers **Open report** (opens the HTML in your default browser).

### Typical workflow

```
Creo running → CREOSON running → Open this app → Choose folder & output → Extract
```

**Shutdown (when you are done):** close this app, stop CREOSON, then exit Creo. If **Stop CREOSON** hangs, exit Creo first, then stop CREOSON or end its process in Task Manager.

The log should show `Checking TCP localhost:9056 …` then `Connecting to CREOSON …` without a long timeout. A ~60s timeout usually means Creo was not running or CREOSON was not linked to it.

## Which `.prt` files are processed?

### Where the app looks

| **Recursively find files** | Behavior                                                                 |
| -------------------------- | ------------------------------------------------------------------------ |
| **Off** (default)          | Only files directly in the **Models folder** (not in subfolders).          |
| **On**                     | Files in the models folder **and every subfolder** beneath it.             |

### Version pick (same folder)

For each logical part name in a given folder:

- If only **`name.prt`** exists, that file is used.
- If only versioned files exist (`name.prt.5`, …), the **highest number** is used (e.g. `name.prt.10` over `name.prt.9`).
- If **`name.prt`** and versioned files both exist, the **highest version** is used (e.g. `name.prt.10` over `name.prt` and `name.prt.9`).

With recursion enabled, `subasm\bracket.prt` and `hardware\bracket.prt` are **two separate** parts (grouped per folder + name).

Files that are only `*.prt.N` on disk (e.g. `ec-j1000-0011.prt.7`) are copied as a temp `name.prt` in the **same folder as your HTML output** before open (Creo cannot open `.prt.N` by name through CREOSON). Creo’s working directory is that output folder, not the network models folder.

### Part name in the report

The **Part name** column always shows the filename without a version suffix (e.g. `bracket.prt`, not `bracket.prt.7`), whether or not recursive search is enabled. Dragging supplies a full absolute `file:///…` path with the plain `.prt` name so Creo opens the latest file on disk. Clicking does nothing. **Parameter values** in the row are read from the model file that was actually opened (e.g. `name.prt.7` when that is the file on disk).

Models are opened in **non-display** mode for stable batch automation (no on-screen flash per part).

## HTML report

The output is one **self-contained** `.html` file: styles and scripts are embedded (no separate `.css` or `.js` files). You can copy or email the file; search and sort work offline.

### Table layout

| Column        | Content                                                                 |
| ------------- | ----------------------------------------------------------------------- |
| **Part name** | Part filename as a link (click does nothing). **Drag** the name into Creo to open the model; a tooltip on the link reminds you. Drag always uses a full absolute `file:///…` URI (`part.prt` when the model is `part.prt.N`). |
| *Parameter columns* | One column per requested name, in the order you listed them. If **Parameters** was blank, one column per distinct parameter name found across all successful parts (union, in Creo list order). |

The page title is **Creo Parameter Extractor Report**. Failed parts appear as highlighted rows; a summary **Errors** list appears at the bottom when needed.

### In the browser

- **Search** — filter rows by text; choose **All fields** or a single column. Click **Search** or press Enter. **Clear** resets the search box and field to **All fields** and shows every row again.
- **Sort** — click a column header to sort ascending; click again for descending. ▲ / ▼ shows the active column.
- **Field dropdown** — your last choice is remembered in the browser (`localStorage`) for the next report you open in that browser.

### Parameters list (recap)

- **Named list:** `PART_NO, DESCRIPTION, MATERIAL` — only those columns appear; missing values are empty cells.
- **Blank:** all parameters from each model; column set is the union of every name seen on any successful part.
- Names are matched **case-insensitively** (Creo treats parameter names that way).

### Settings file

UI choices are saved to `prt_parameter_extractor_settings.json`. The file is created beside the app or under `%APPDATA%\CreoParameterExtractor\` if the install folder is not writable.

Example keys:

| Key                 | Purpose                                      |
| ------------------- | -------------------------------------------- |
| `models_folder`     | Last models directory                        |
| `parameter_names`   | Comma-separated list (empty = all parameters) |
| `output_file`       | Last HTML report path                        |
| `creoson_host`      | CREOSON hostname                             |
| `creoson_port`      | CREOSON port (default `9056`)                |
| `recursive_search`  | `true` / `false` — **Options** menu checkbox |

## Troubleshooting

| Symptom                                                 | What to try                                                                                                                      |
| ------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| Hangs at “Connecting to CREOSON…” then times out (~60s) | Start Creo first, then CREOSON. Dismiss any Creo modal dialogs.                                                                  |
| `Unknown Model Extension`                               | Usually an old issue with opening `.prt.N` directly; use the current version of this tool.                                       |
| `Pro/TOOLKIT … General Error`                           | Open the part manually in Creo; fix regen errors or missing references. Ensure related files sit in the same folder as the part. |
| CREOSON **Stop** hangs                                  | Exit Creo first, then stop CREOSON; or end the CREOSON/Java process in Task Manager.                                             |
| `0 .prt file(s) found`                                  | Folder path wrong; no matching files in that directory; or parts are in **subfolders** — enable **Options → Recursively find files**. |
| UNC path (`\\server\share\...`) or `creo_cd` fails on network library | Point **Models folder** at the share, but set **Output file** to a **local** path (e.g. `C:\reports\out.html`). Creo/CREOSON use only that output folder for temp copies. UNC as models folder is OK for reading; Creo never uses UNC as WD. |
| CREOSON: `Directory does not exist` for `Z:\...`        | Same: use a **local** output path. Models are read from `Z:\` (or UNC) and copied as temp files next to the HTML report. |

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

   **Executable** (after building with `build_pyinstaller.bat`, or if `CreoParameterExtractor.exe` is in the project folder):

   ```bat
   CreoParameterExtractor.exe
   ```

   **From source** (after step 3 above):

   ```bat
   python prt_parameter_extractor.py
   ```

### CREOSON: where to get it and install

CREOSON is a small **JSON server** between this app and **Creo Parametric**. It translates JSON commands (open file, list parameters, etc.) into **J-Link** calls inside Creo. This tool talks to CREOSON over HTTP on the port you set (default **9056**); it does not replace CREOSON.

#### Download

| Source                     | URL                                                                                                |
| -------------------------- | -------------------------------------------------------------------------------------------------- |
| **Releases (recommended)** | [github.com/SimplifiedLogic/creoson/releases](https://github.com/SimplifiedLogic/creoson/releases) |
| **Source / docs**          | [github.com/SimplifiedLogic/creoson](https://github.com/SimplifiedLogic/creoson)                   |
| **API reference**          | [creoson.com/functions.html](https://creoson.com/functions.html)                                   |

For most users, download **CreosonServerWithSetup-\*.zip** (setup GUI, includes Java). Choose **32-bit** or **64-bit** to match your Creo install.

#### Install CREOSON (pre-packaged)

CREOSON is not a traditional installer—you unzip and run from a folder:

1. Download **CreosonServerWithSetup-\*.zip** from [Releases](https://github.com/SimplifiedLogic/creoson/releases).
2. Copy the ZIP to a permanent location (e.g. `C:\Tools\creoson\`) and **extract** it.
3. Run **CreosonSetup.exe**.
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
