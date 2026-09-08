const {parse} = require('node:url');

const policies = new Map([
  ['GET /api/jobs/metadata', 64],
  ['GET /api/jobs/metadata/detail', 96],
  ['GET /api/jobs/metadata/local', 32],
  ['GET /api/jobs/metadata/local/detail', 32],
  ['GET /api/jobs/metadata/view', 16],
  ['POST /api/jobs/metadata/pins', 64],
  ['POST /api/jobs/metadata/view/refresh', 16],
].map(([route, kib]) => [route, Object.freeze({
  maxRequestBytes: 64 * 1024,
  maxResponseBytes: kib * 1024,
  timeoutMs: 30000,
})]));

function jobMetadataPolicy(method, apiPath) {
  // Match Handler's urlparse(...).path: no dot resolution or escape decoding,
  // and final-segment parameters are separate from the path. Python's HTTP
  // handler also collapses leading slashes before routing. Invalid input is
  // left to the request helper's sanitized transport errors.
  if (typeof apiPath !== 'string') return undefined;
  try {
    const pathname = parse(apiPath.replace(/^\/{2,}/, '/')).pathname;
    return policies.get(`${method} ${pathname?.replace(/;[^/]*$/, '')}`);
  } catch {
    return undefined;
  }
}

module.exports = {jobMetadataPolicy};
