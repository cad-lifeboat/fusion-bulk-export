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
In Fusion press Shift+S (Utilities > Add-Ins > Scripts and Add-Ins). Find
FusionBulkExport in the list: type its name in Search, or tick Add-ins under Type
on the left. Tick its Run on Startup box first, then switch Run on. The window
closes when you switch it on; that is normal.

If FusionBulkExport is not in the list (or you prefer not to run the installer):
move the FusionBulkExport folder somewhere it can stay, such as Documents, then
click the "+" at the top of the list and choose the add-in's folder. Tick Run on
Startup first, then switch Run on, as above.


3. Export
---------
Open the Utilities tab and click Fusion Bulk Export (the orange cube in the
ADD-INS group). Check "Save to" (Browse... picks another folder; it is remembered),
choose Everything or tick projects and folders, and click Export. "Also save other
files (PDFs, uploads)" is on by default. Fusion cannot be used while it exports:
leave it open and signed in until the summary appears. It gives the counts and the
paths of the output folder and of index.html, a report of everything saved and
anything that failed. Drawings and electronics are counted as "can't be exported
by an add-in": not failures.
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
Long runs can make Fusion use more memory. If Fusion itself holds too much for a
while, the export stops itself; close Fusion, open it again and export to the same
folder to carry on. If other programs use up the memory instead, it goes on, and when the
computer stays critically short of memory the report says that closing other
programs may speed it up.


Help
----
Instructions, questions and bug reports:
https://github.com/cad-lifeboat/fusion-bulk-export

Your output folder's manifest.json, index.html, index.csv and logs contain your
design names. Keep them private and do not attach them to issues. errors.log in the
add-in folder (and the error text the dialog offers to copy) leaves out paths and
project and design names, so it can be pasted into an issue; read it first anyway.

Not affiliated with Autodesk. Autodesk and Fusion are trademarks of Autodesk, Inc.
