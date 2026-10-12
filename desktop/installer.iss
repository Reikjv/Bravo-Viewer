; Instalador do Bravo Viewer Desktop.
; Build: ISCC.exe /DAppVersion=0.4.1-beta.1 /DAppNumeric=0.4.1.0 desktop\installer.iss
; Instalação silenciosa (hub de programas): BravoViewerDesktop-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
; O AppId é fixo: novas versões atualizam a instalação existente em vez de duplicá-la.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef AppNumeric
  #define AppNumeric "0.0.0.0"
#endif

[Setup]
AppId={{396F7919-63B9-4606-AC48-F8D1089C2E25}
AppName=Bravo Viewer Desktop
AppVersion={#AppVersion}
AppVerName=Bravo Viewer Desktop {#AppVersion}
AppPublisher=Reikjv
AppPublisherURL=https://github.com/Reikjv/Bravo-Viewer
AppSupportURL=https://github.com/Reikjv/Bravo-Viewer
VersionInfoVersion={#AppNumeric}
VersionInfoProductTextVersion={#AppVersion}
DefaultDirName={autopf}\Bravo Viewer
DefaultGroupName=Bravo Viewer
DisableProgramGroupPage=yes
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
SetupIconFile=..\catalog\bravo.ico
UninstallDisplayIcon={app}\BravoViewerDesktop.exe
UninstallDisplayName=Bravo Viewer Desktop
OutputDir=..\artifacts\desktop
OutputBaseFilename=Bravo-Viewer-Desktop-v{#AppVersion}-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na Área de Trabalho"; GroupDescription: "Atalhos:"

[Files]
Source: "..\dist\BravoViewerDesktop\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Bravo Viewer"; Filename: "{app}\BravoViewerDesktop.exe"
Name: "{autodesktop}\Bravo Viewer"; Filename: "{app}\BravoViewerDesktop.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\BravoViewerDesktop.exe"; Description: "Abrir o Bravo Viewer"; Flags: nowait postinstall skipifsilent

; Os dados sincronizados ficam em %LOCALAPPDATA%\BravoViewerDesktop e são preservados na desinstalação.
