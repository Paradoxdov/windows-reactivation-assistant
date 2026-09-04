# Portable Windows Reactivation Assistant

A portable assistant for IT professionals restoring activation on licensed
Windows PCs through Microsoft's Product Activation Portal. It handles the
repetitive preparation: reading licensing information, retrieving the
Installation ID, navigating the portal and filling in the form.

**The tool automatically retrieves your Installation ID from Windows, fills it
into Microsoft's activation portal, and verifies the entered value.** It then
pauses before Submit so you can review and continue manually.

This is an independent project, not affiliated with Microsoft. It does not
bypass licensing. The current version does not install product keys or apply
a Confirmation ID automatically.

## Why use it?

- **Portable:** run the executable without installing Python or a separate WebDriver.
- **Less repetitive work:** retrieve the Installation ID and prepare the portal form automatically.
- **Operator control:** review the completed form before submitting it manually.
- **Official workflow:** use Microsoft's portal, not a third-party activation service.
- **Dedicated browser profile:** keep the servicing session separate from the user's regular Edge profile.
- **Diagnostics and privacy:** redacted logs, named errors and guarded local-data cleanup.

## Who is it for?

- PC repair technicians and service centres.
- System administrators and internal IT support teams.
- Managed service providers and on-site IT specialists.
- Technicians servicing licensed PCs after repairs, hardware changes or Windows reinstallation.

## How version 1.0 works

1. Reads the Windows edition, build and licensing information.
2. Retrieves the Offline Installation ID from Windows.
3. Opens Microsoft Edge with a dedicated local profile.
4. Opens the official Microsoft Product Activation Portal.
5. Waits while the operator completes CAPTCHA and sign-in, if required.
6. Selects the product, Windows version or Installation ID block size.
7. Fills in the Installation ID and verifies the entered value.
8. Locates Submit and hands control back to the operator.

The assistant **does not click Submit or call a submission endpoint directly**.
Once a value is entered into a web form, it should nevertheless be treated as
available to the portal's own scripts. The current program does not change the
PC's licensing state.

Licensing queries are read-only and use `SoftwareLicensingProduct`. The current
version does not invoke `slmgr /ipk`, `/upk`, `/cpky`, `/ato` or `/atp`.

## Installation ID and Confirmation ID

These are two different identifiers used at different steps:

- **Installation ID (IID)** comes from Windows. The tool already retrieves it
  automatically, fills it into the portal, and verifies the entered value.
- **Confirmation ID (CID)** is the response provided by Microsoft after the
  submitted request is accepted. It is then applied in Windows to complete
  activation. Confirmation ID retrieval and application are not automated in
  the current version.

The operator reviews the filled form, clicks Submit, and follows the portal's
instructions to complete the remaining steps manually.

## Requirements

- Windows 10 or Windows 11.
- Microsoft Edge installed.
- A Windows license eligible for the official offline activation workflow.
- Connectivity to Microsoft's services during the portal steps.

## Download and run

Download `WindowsReactivationAssistant.exe` and `SHA256SUMS.txt` from
[Releases](https://github.com/Paradoxdov/windows-reactivation-assistant/releases/latest).

The executable is **not digitally signed**. Windows may show a warning.
Check its SHA-256 against the published checksum before running it.

1. Copy the EXE to a dedicated folder on the serviced PC or a USB drive.
2. Run it on the PC being serviced.
3. Complete CAPTCHA and Microsoft account sign-in manually in Edge, if requested.
4. Review the Installation ID and click Submit manually.
5. Continue the official process manually. Version 1.0 does not retrieve or
   apply the Confirmation ID for you.

## Run from source

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python src\main.py
```

## Build the executable

```powershell
.\.venv\Scripts\python -m pip install -r requirements-build.txt
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

The build script uses `.venv\Scripts\python.exe` when available, otherwise
`python` from PATH. You can also pass `-Python <path-to-python.exe>`.

Build output is placed in `release\`, including the EXE, README and
`SHA256SUMS.txt`.

## Command-line options

| Option | Purpose |
|---|---|
| No options | Run the normal workflow |
| `--license-only` | Read Windows, license and IID information without opening the browser |
| `--verbose` | Include detailed console output |
| `--keep-profile` | Keep the Microsoft session instead of erasing it at the end |
| `--close-browser` | Close Edge after the handoff point |
| `--reset-profile` | Remove the dedicated browser profile and sign out |
| `--cleanup` | Attempt to remove the profile, logs, temporary files and matching shell history |
| `--write-config` | Create a local `config.json` with default settings |
| `--no-pause` | Exit without waiting for Enter |

## Local data and privacy

The following may be created next to the executable:

- `BrowserProfile\`: the dedicated Edge profile.
- `Logs\`: diagnostic logs.
- `Temp\`: Edge temporary files.
- `config.json`: optional local settings.

After an interactive session, the assistant attempts to erase its Microsoft
sign-in profile by default. A crash or non-interactive exit can leave the
profile behind. Use `--cleanup` before handing the drive to another person.

The program avoids logging credentials and full identifiers, and masks
product keys, Installation IDs, email addresses and other sensitive patterns.
Even with redaction, treat browser profiles and logs as sensitive local data.
Profiles, logs, configuration and build output are excluded from Git.

Session erasure and `--cleanup` also attempt to remove matching current-user
Windows shell-history entries: FeatureUsage, UserAssist, JumplistData and
jump-list files associated with the dedicated Edge profile. Cleanup is
best-effort; locked files or access restrictions can prevent completion.
`--cleanup` returns a nonzero code for detected failures. It keeps
`config.json` and does not guarantee removal of every possible Windows trace.

To guard against accidental deletion, `profile_dir` must be a dedicated local
subfolder inside the program directory, outside `Temp\`. External paths,
redirected directories and unrecognised non-empty folders are rejected.

## Error codes

| Code | Meaning |
|---|---|
| `EDGE_NOT_FOUND` | Microsoft Edge was not found |
| `EDGE_LAUNCH_FAILED` | Edge did not open its automation port |
| `INSTALLATION_ID_NOT_AVAILABLE` | Windows did not provide an Installation ID |
| `MICROSOFT_LOGIN_REQUIRED` | Manual sign-in is required |
| `HUMAN_VERIFICATION_REQUIRED` | Manual CAPTCHA completion is required |
| `PORTAL_TIMEOUT` | A portal step timed out |
| `PORTAL_LAYOUT_CHANGED` | The portal layout was not recognised |
| `IID_FIELD_NOT_FOUND` | An Installation ID field was not found |
| `IID_INSERT_FAILED` | The entered Installation ID could not be verified |
| `SUBMIT_BUTTON_NOT_FOUND` | Submit could not be located |
| `UNSAFE_PROFILE_DIRECTORY` | The configured profile directory is unsafe or not owned by the assistant |
