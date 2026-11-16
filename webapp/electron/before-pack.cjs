"use strict";
const writeBootstrapRevision = require("./write-bootstrap-revision.cjs");
const { buildUniversalMacHelper, buildUniversalSpeechHelper } = require("./build-native-computer.cjs");
let nativeBuild;
let speechBuild;

module.exports = async context => {
  const metadata = writeBootstrapRevision(context);
  if (context.electronPlatformName === "darwin") {
    nativeBuild ||= buildUniversalMacHelper();
    await nativeBuild;
    // Voice dictation is optional: an SDK older than macOS 26 cannot build it,
    // and the app then reports on-device dictation as missing from this build.
    speechBuild ||= buildUniversalSpeechHelper().catch(error => {
      console.warn(`speech-macos helper not built: ${error.message}`);
    });
    await speechBuild;
  }
  return metadata;
};
