Fusion Bulk Export {version}
============================

Saves your Autodesk Fusion designs to your own computer, as Fusion archives (.f3d)
and STEP files under their real names, in your project and folder layout, together
with your other files (PDFs, uploads). Drawings and electronics are not supported
(export those from Fusion with File > Export). It only reads: nothing in your Fusion
account is changed.

You need Fusion installed and signed in, on macOS or Windows. Nothing else.


1. Install
----------
macOS:   double-click "Install (macOS).command".
         It is not signed, so the first time macOS (Gatekeeper) refuses to open it:
         right-click it, choose Open, then Open again. If there is no Open button, go
         to System Settings > Privacy & Security, scroll down and click Open Anyway.
Windows: double-click "Install (Windows).bat".
         If Windows SmartScreen warns you, click More info, then Run anyway.

The installer copies the FusionBulkExport folder into Fusion's add-ins folder.
After that you can delete this download.


2. Switch it on in Fusion (once)
--------------------------------
In Fusion press Shift+S (or Utilities > Add-Ins > Scripts and Add-Ins), open the
Add-Ins tab, switch FusionBulkExport on (Run) and tick Run on Startup.

If FusionBulkExport is not in the list (or you prefer not to run the installer):
move the FusionBulkExport folder somewhere it can stay, such as Documents, then in
the Add-Ins tab click the green "+" and choose that folder.


3. Export
---------
Open the Utilities tab and click Fusion Bulk Export (the orange cube in the
ADD-INS group). Check "Save to" (Browse... picks another folder; it is remembered),
choose Everything or tick projects and folders, and click Export. "Also save other
files (PDFs, uploads)" is on by default. Fusion cannot be used while it exports:
leave it open and signed in until the summary appears. It gives the counts and the
paths of the output folder and of index.html, a report of everything saved and
anything that failed. Drawings and electronics are counted as "not supported by
Fusion's export": not failures.
If the run gets into trouble (designs failing in a row, a full disk) it pauses and
asks "Try again now?": OK carries on, Cancel stops. Run it again on the same folder to
continue, retry failures or pick up new designs.

On Windows, keep the output folder short (such as C:\FusionExport); its own path can
be at most 200 characters. Names are cut at 64 characters, and a file whose full path
is still over 260 characters is saved but may not open from Explorer; the summary and
index.html say how many there are.


Big account?
------------
With hundreds or thousands of designs, export one project or folder first and look
at the result. Then do the rest into the same output folder: finished designs are
skipped, so nothing is done twice. Time varies a lot with design size.


Help
----
Instructions, questions and bug reports:
https://github.com/cad-lifeboat/fusion-bulk-export

Your output folder's manifest.json, index.html, index.csv and logs contain your
design names. Keep them private and do not attach them to issues. errors.log in the
add-in folder (and the error text the dialog offers to copy) leaves out paths and
project and design names, so it can be pasted into an issue; read it first anyway.

Not affiliated with Autodesk. Autodesk and Fusion are trademarks of Autodesk, Inc.
