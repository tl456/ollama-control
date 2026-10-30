; Build with scripts/build_release.py installer after freeze and prepare.
#define ProjectRoot AddBackslash(SourcePath) + ".."
#ifndef AppVersion
  #define AppVersion "1.6.0"
#endif
#ifndef FrozenDir
  #define FrozenDir ProjectRoot + "\dist\LocalAIStudio"
#endif
#ifndef BuildMetadataDir
  #define BuildMetadataDir ProjectRoot + "\dist\build-metadata"
#endif
#ifndef ReleaseDir
  #define ReleaseDir ProjectRoot + "\dist\installer"
#endif

[Setup]
AppId={{B5DFBE03-0A13-4E5C-86EB-2FAF4EC2E391}
AppName=本地智能工作台
AppVersion={#AppVersion}
AppPublisher=tl456
AppCopyright=Copyright (c) 2026 tl456. MIT License.
SetupIconFile={#ProjectRoot}\web\app.ico
DefaultDirName={localappdata}\LocalAIStudio
DefaultGroupName=本地智能工作台
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
DisableProgramGroupPage=yes
AllowNoIcons=yes
CloseApplications=no
RestartApplications=no
UninstallDisplayIcon={app}\LocalAIStudio.exe
WizardStyle=modern dynamic
WizardSizePercent=115
Compression=lzma2/max
LZMANumBlockThreads=4
SolidCompression=yes
OutputDir={#ReleaseDir}
OutputBaseFilename=本地智能工作台-{#AppVersion}-完整安装
DiskSpanning=yes
DiskSliceSize=1900000000
SetupLogging=yes
UninstallLogMode=append
VersionInfoDescription=本地智能工作台

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式（BAT 启动）"; GroupDescription: "快捷方式："

[Dirs]
Name: "{app}\config"; Flags: uninsneveruninstall
Name: "{app}\data"; Flags: uninsneveruninstall
Name: "{app}\models"; Flags: uninsneveruninstall
Name: "{app}\logs"; Flags: uninsneveruninstall
Name: "{app}\cache"; Flags: uninsneveruninstall
Name: "{app}\plugins\imported"; Flags: uninsneveruninstall

[Files]
; All provided official Ollama libraries remain in the prepared payload.
; Models are separate so GGUF layers can be stored without recompression.
Source: "{#FrozenDir}\*"; DestDir: "{app}"; Excludes: "\models\*,__pycache__\*,*.pyc,*.pyo"; Flags: ignoreversion recursesubdirs createallsubdirs
#include AddBackslash(BuildMetadataDir) + "model-files.iss"

[Icons]
Name: "{group}\本地智能工作台"; Filename: "{app}\启动.bat"; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"
Name: "{group}\浏览器模式"; Filename: "{app}\浏览器启动.bat"; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"
Name: "{autodesktop}\本地智能工作台"; Filename: "{app}\启动.bat"; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\LocalAIStudio.exe"; Description: "启动本地智能工作台"; Flags: nowait postinstall skipifsilent

[Code]
function NextButtonClick(CurPageID: Integer): Boolean;
var
  Index: Integer;
  Directory: String;
begin
  Result := True;
  if (CurPageID <> wpSelectDir) or WizardSilent then Exit;
  Directory := WizardDirValue;
  for Index := 1 to Length(Directory) do
    if Ord(Directory[Index]) > 127 then
    begin
      Result := MsgBox('建议将程序安装到完整英文路径，例如 D:\LocalAIStudio。' + #13#10 +
        '部分 Ollama 显卡运行库在中文程序路径下可能加载失败。' + #13#10 +
        '模型与资料文件名可以使用中文。仍继续使用当前目录吗？', mbConfirmation, MB_YESNO) = IDYES;
      Exit;
    end;
end;
