; Inno Setup 6 script for LaFlasher.
;
; Normally compiled by build.py, which passes the version and paths:
;     python build.py
; To compile by hand after a folder build (dist\LaFlasher):
;     ISCC installer\LaFlasher.iss

#define AppName "LaFlasher"
#define AppExeName "LaFlasher.exe"
#define AppPublisher "lab.krts"
#define AppURL "https://krts.dev/laflasher"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef SourceDir
  #define SourceDir SourcePath + "..\dist\LaFlasher"
#endif
#ifndef BuildDir
  #define BuildDir SourcePath + "..\build"
#endif
#ifndef OutputDir
  #define OutputDir SourcePath + "..\dist"
#endif
#ifndef WizardLarge
  #define WizardLarge BuildDir + "\wizard-large-100.bmp"
#endif
#ifndef WizardSmall
  #define WizardSmall BuildDir + "\wizard-small-100.bmp"
#endif

[Setup]
; Keep AppId the same forever: it is how Windows recognises upgrades of this app.
AppId={{A72BFF42-F490-4C98-BBB7-EBE4A54E43B7}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
; Per-user install by default (no admin prompt); the user can choose "all users" instead.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir={#OutputDir}
OutputBaseFilename={#AppName}-{#AppVersion}-setup
SetupIconFile={#BuildDir}\laflasher.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
WizardStyle=modern
WizardImageFile={#WizardLarge}
WizardSmallImageFile={#WizardSmall}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
ChangesAssociations=yes
CloseApplications=yes
RestartApplications=no
UsedUserAreasWarning=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "associate"; Description: "Open .laflash packages with {#AppName}"; GroupDescription: "File types:"

[InstallDelete]
; Upgrades: clear the previous version's bundled libraries first so no stale files remain.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourcePath}..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
; .laflash file type (HKA = this user, or all users when installed for everyone)
Root: HKA; Subkey: "Software\Classes\.laflash"; ValueType: string; ValueName: ""; ValueData: "LaFlasher.Package"; Flags: uninsdeletevalue uninsdeletekeyifempty; Tasks: associate
Root: HKA; Subkey: "Software\Classes\.laflash\OpenWithProgids"; ValueType: string; ValueName: "LaFlasher.Package"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty; Tasks: associate
Root: HKA; Subkey: "Software\Classes\LaFlasher.Package"; ValueType: string; ValueName: ""; ValueData: "LaFlasher firmware package"; Flags: uninsdeletekey; Tasks: associate
Root: HKA; Subkey: "Software\Classes\LaFlasher.Package\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"",0"; Tasks: associate
Root: HKA; Subkey: "Software\Classes\LaFlasher.Package\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""; Tasks: associate
; The app's own settings (window size, recent packages) are removed on uninstall.
Root: HKCU; Subkey: "Software\L12\LaFlasher"; Flags: uninsdeletekey dontcreatekey

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
