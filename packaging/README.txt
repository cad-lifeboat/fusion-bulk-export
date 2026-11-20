Fusion Bulk Export {version}
============================

Saves your Autodesk Fusion designs to your own computer, as Fusion archives (.f3d)
and STEP files, in your project and folder layout. It only reads: nothing in your
Fusion account is changed.

You need Fusion installed and signed in, on macOS or Windows. Nothing else.


1. Install
----------
macOS:   double-click "Install (macOS).command".
         If macOS says it cannot check the file, right-click it, choose Open, then
         Open again (or allow it under System Settings > Privacy & Security).
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
ADD-INS group). Choose Everything, or tick projects and folders, choose an output
folder and click Export. Leave Fusion open and signed in until the last dialog.
Run it again on the same folder to continue, retry failures or pick up new designs.


Big account?
------------
With hundreds or thousands of designs, export one project or folder first and look
at the result. Then do the rest into the same output folder: finished designs are
skipped, so nothing is done twice. Time varies a lot with design size.


Help
----
Instructions, questions and bug reports:
https://github.com/cad-lifeboat/fusion-bulk-export

Your output folder's manifest.json and logs contain your design names. Keep them
private and do not attach them to issues.

Not affiliated with Autodesk. Autodesk and Fusion are trademarks of Autodesk, Inc.
