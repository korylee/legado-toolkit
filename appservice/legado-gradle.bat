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
if "%JAVA_HOME%"=="" set "JAVA_HOME=D:\Program Files\Java\jdk-21.0.12.1+1"
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
