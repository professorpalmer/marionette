"use strict";
const writeBootstrapRevision = require("./write-bootstrap-revision.cjs");
const { buildUniversalMacHelper } = require("./build-native-computer.cjs");
let nativeBuild;

module.exports = async context => {
  const metadata = writeBootstrapRevision(context);
  if (context.electronPlatformName === "darwin") {
    nativeBuild ||= buildUniversalMacHelper();
    await nativeBuild;
  }
  return metadata;
};
