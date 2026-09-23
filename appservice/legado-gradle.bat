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

REM Runtime paths are resolved once by core/jvm_env.py and passed to this launcher.
if "%LEGADO_REPO%"=="" (
  echo [error] LEGADO_REPO is not set; run the JVM environment self-test first.
  exit /b 1
)
if "%JAVA_HOME%"=="" (
  echo [error] JAVA_HOME is not set; run the JVM environment self-test first.
  exit /b 1
)
if "%ANDROID_HOME%"=="" (
  echo [error] ANDROID_HOME is not set; run the JVM environment self-test first.
  exit /b 1
)
if "%GRADLE_USER_HOME%"=="" (
  echo [error] GRADLE_USER_HOME is not set; run the JVM environment self-test first.
  exit /b 1
)
if not exist "%JAVA_HOME%\bin\java.exe" (
  echo [error] java.exe not found under JAVA_HOME: %JAVA_HOME%
  exit /b 1
)
if not exist "%ANDROID_HOME%" (
  echo [error] Android SDK not found: %ANDROID_HOME%
  exit /b 1
)
echo [appservice] JAVA_HOME=%JAVA_HOME%
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
REM **必须把 Gradle 的退出码带出去**：原来结尾是 endlocal，于是这个 bat 永远
REM 返回 0——调用方（backend/api/jvm.py 的 `ok = code == 0`、A1/A4 的调试
REM 「零事件=退出非 0」）都只能拿到一个假的成功。set 之后立刻取，别插别的
REM 命令（任何命令都可能改 ERRORLEVEL）。`endlocal & exit /b %RC%` 是标准写法：
REM %RC% 在解析这一行时就展开，所以 endlocal 清掉变量也不影响。
set "RC=%ERRORLEVEL%"
popd
endlocal & exit /b %RC%
