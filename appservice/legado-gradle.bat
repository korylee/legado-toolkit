@echo off
REM Run Gradle inside the Legado app repo while keeping ALL of our sources
REM in legado-source/appservice/. The app repo stays pristine: no new files
REM are created in it (only its build/ output, which is gitignored).
REM
REM Usage (same args as gradlew, run from anywhere):
REM     legado-gradle.bat :app:testDebugUnitTest --tests "io.legado.app.WebBookProbeTest"
REM
REM Override LEGADO_REPO if the app repo lives elsewhere.
REM
REM NOTE: this file MUST keep CRLF line endings. Mixed LF/CRLF makes cmd
REM splice lines together, which silently corrupts the gradle arguments.
setlocal

if "%LEGADO_REPO%"=="" set "LEGADO_REPO=D:\Documents\GitHub\legado-with-MD3"
REM JAVA_HOME: honor a **usable** one (shell / vfox export), else derive it.
REM Judging by "java.exe exists" rather than "variable is non-empty" is
REM deliberate: an empty or whitespace JAVA_HOME is worse than an unset one --
REM gradlew would abort with "invalid directory" instead of falling through.
REM vfox keeps SDKs under %USERPROFILE%\.vfox\sdks\java (JDK root directly, or
REM <version> subdirs when several are installed). The backend also passes a
REM discovered JAVA_HOME via the environment (core/jvm_env.py).
if exist "%JAVA_HOME%\bin\java.exe" goto java_ready
if exist "%USERPROFILE%\.vfox\sdks\java\bin\java.exe" (
  set "JAVA_HOME=%USERPROFILE%\.vfox\sdks\java"
  goto java_ready
)
for /d %%D in ("%USERPROFILE%\.vfox\sdks\java\*") do (
  if not defined JAVA_FROM_VFOX if exist "%%D\bin\java.exe" (
    set "JAVA_FROM_VFOX=1"
    set "JAVA_HOME=%%D"
  )
)
:java_ready
if not exist "%JAVA_HOME%\bin\java.exe" set "JAVA_HOME=D:\Program Files\Java\jdk-21.0.12.1+1"
echo [appservice] JAVA_HOME=%JAVA_HOME%
if "%GRADLE_USER_HOME%"=="" set "GRADLE_USER_HOME=D:\.gradle"
if "%ANDROID_HOME%"=="" set "ANDROID_HOME=D:\Android\Sdk"
set "LEGADO_APPSERVICE_DIR=%~dp0"

if not exist "%LEGADO_REPO%\gradlew.bat" (
  echo [error] app repo not found: %LEGADO_REPO%
  echo         set LEGADO_REPO to point at it.
  exit /b 1
)

REM Do NOT use -p: the working directory must be the repo root. Gradle
REM computes its test class-to-source mapping relative to the CWD; with -p
REM the CWD stays elsewhere and test discovery silently breaks
REM ("No tests found", for the app's own tests too). pushd instead.
pushd "%LEGADO_REPO%"
call gradlew.bat -I "%~dp0legado-test.init.gradle" %*
popd
endlocal
