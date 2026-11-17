# Community Calendar: Quick Start

Create an agent that reads event fliers, then use it in chat or an upload app.
It returns **JSON**: structured text containing dates, times, and event details.
This does not build a calendar website.

Setup from [prereq-instructions.txt](../prereq-instructions.txt) is already done.
Keep the `pronet` project folder open in VS Code.

## 1. Create the agent

1. Open **Copilot Chat** and select the built-in **Agent** mode in a local chat.
2. Drag [1.CreateAgentPrompt.txt](data/1.CreateAgentPrompt.txt) from the Explorer
   into the chat.
3. Send:

   ```text
   Use the attached prompt to create .github\agents\FlierReaderAgent.agent.md
   in the pronet project root. Name it FlierReaderAgent and add a description.
   Keep all extraction rules and the complete JSON structure.
   Create only this file; do not install anything or change other files.
   If the file already exists, ask before replacing it.
   ```

4. Review any permission requests and the created file. Save with **Ctrl+S**.
5. Start a new chat and select **FlierReaderAgent** from the agent dropdown.

**Agent missing?** Check that the file is saved in the project's
`.github\agents` folder, not inside `CommunityCal`. Then press **Ctrl+Shift+P**
and run **Developer: Reload Window**.

## 2. Test two fliers

1. With **FlierReaderAgent** selected, choose a model that supports images.
2. Drag [flier1.jpg](data/flier1.jpg) into the chat. Make sure the image is
   attached; typing its path is not enough.
3. Send:

   ```text
   Extract the events from this flier using your required JSON structure.
   Return valid JSON only.
   ```

4. Compare the result with the image and the checks below.
5. Repeat in a **new chat** with [flier2.jpg](data/flier2.jpg), selecting the
   same agent and an image-capable model.

| Check | First flier | Second flier |
| --- | --- | --- |
| Event count | 1 | 1 |
| `GregorianDate` | `2026-10-22` | `2026-10-24` |
| `StartTime` | `19:30` | `20:00` |
| Program | English; starts with Isha prayer | Urdu; Noha Special Guest |
| Speaker | HI Sayyid Sulayman Hasan | HI Sayed Raza Hussain Rizvi |
| Noha participant | None printed | Ali Zia Rizvi |

Neither flier shows an end time or Hijri date: `EndTime` and every
`IslamicDate` field should be `null` (missing).
Also check the remaining fields against the image and prompt.
If anything is wrong, note it, ask Copilot to fix the agent, and test again.

## 3. Run the upload app

1. In VS Code, choose **Terminal > New Terminal**. Use PowerShell in the
   `pronet` project folder and run:

   ```powershell
   .\.venv\Scripts\python.exe -m uvicorn CommunityCal.app:app --host 127.0.0.1 --port 8000
   ```

2. Open [http://127.0.0.1:8000](http://127.0.0.1:8000).
3. Choose a JPEG, PNG, or WebP image (up to 10 MB and 20 million pixels).
   Click **Extract event data**.
4. Check the result and warnings against the image. Click **Download JSON**
   to save it.
5. To stop the app, press **Ctrl+C** in the terminal.

## 4. Create events in Google Calendar

Run a flier through and validate it shows up in Google Cal