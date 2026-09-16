"use strict";

const OPERATIONS = new Set(["status", "apps", "snapshot", "click", "type", "keypress", "scroll"]);
const INPUTS = new Set(["click", "type", "keypress", "scroll"]);

function createComputerController({ createNative, requestApproval, onState = () => {} }) {
  let sessionId = "";
  let generation = 0;
  let native;
  let pending;
  let busy = false;
  const grants = new Map();
  const denied = new Set();
  const approvals = new Set();
  const publish = () => onState({ sessionId, apps: [...grants.values()].map(app => ({ app_id: app.app_id, name: app.name })), pending: approvals.size > 0 });
  function revoke() {
    generation++;
    pending?.abort();
    native?.close();
    native = undefined;
    grants.clear();
    approvals.clear();
    denied.clear();
    publish();
  }
  function setSession(value) {
    const next = typeof value === "string" && value.length <= 256 ? value : "";
    if (next !== sessionId) { sessionId = next; revoke(); }
  }
  function releaseSession(value) {
    const current = typeof value === "string" && value.length <= 256 ? value : "";
    if (current && current === sessionId) { sessionId = ""; revoke(); }
  }
  async function dispatch(payload, signal) {
    const requested = typeof payload.session_id === "string" ? payload.session_id : "";
    if (requested && requested !== sessionId) setSession(requested);
    if (!sessionId || payload.session_id !== sessionId) throw new Error("Computer control belongs to the active conversation. Switch back before continuing.");
    const args = payload.arguments;
    if (!args || !OPERATIONS.has(args.operation)) throw new Error("Unknown computer operation.");
    if (signal?.aborted) throw new Error("Computer request cancelled.");
    if (busy) throw new Error("Another computer request is in progress.");
    busy = true;
    const epoch = generation;
    const controller = new AbortController();
    const cancel = () => controller.abort();
    signal?.addEventListener("abort", cancel, { once: true });
    pending = controller;
    const current = () => {
      if (controller.signal.aborted || epoch !== generation || payload.session_id !== sessionId) throw new Error("Computer context changed. Take a fresh snapshot.");
    };
    try {
      native ||= createNative();
      if (args.operation === "status" || args.operation === "apps") {
        const result = await native.dispatch(args, controller.signal);
        current();
        return result;
      }
      if (typeof args.app_id !== "string" || !args.app_id) throw new Error("Choose an app_id returned by computer_use apps.");
      const inventory = await native.dispatch({ operation: "apps" }, controller.signal);
      current();
      const apps = Array.isArray(inventory) ? inventory : inventory.apps;
      const target = Array.isArray(apps) && apps.find(app => app.app_id === args.app_id);
      if (!target) throw new Error("The selected application is not running. Do not substitute another app.");
      if (target.pid === process.pid || target.app_id === "bundle:com.marionette.app") throw new Error("The pilot cannot operate Marionette or approve its own permissions.");
      const grant = grants.get(target.app_id);
      if (!grant || grant.pid !== target.pid || grant.instance_id !== target.instance_id) {
        grants.delete(target.app_id);
        if (denied.has(target.app_id)) return { approval: "denied", app_id: target.app_id, message: "The user denied access. Do not retry or use another tool to bypass it." };
        if (!approvals.has(target.app_id)) {
          approvals.add(target.app_id);
          publish();
          Promise.resolve().then(() => requestApproval(target)).then(allowed => {
            if (epoch !== generation || payload.session_id !== sessionId) return;
            approvals.delete(target.app_id);
            if (allowed === true) grants.set(target.app_id, target);
            else denied.add(target.app_id);
            publish();
          }, () => {
            if (epoch !== generation) return;
            approvals.delete(target.app_id);
            denied.add(target.app_id);
            publish();
          });
        }
        return { approval: "pending", app_id: target.app_id, message: "Waiting for the user's app-access decision. Yield to the user; do not poll or bypass the prompt." };
      }
      const result = await native.dispatch(args, controller.signal);
      current();
      if (!INPUTS.has(args.operation)) return result;
      try {
        const state = await native.dispatch({ operation: "snapshot", app_id: target.app_id }, controller.signal);
        current();
        return { action: result, state };
      } catch (error) {
        return { action: result, verification_error: error.message, message: "The input was sent but verification failed. Inspect the app before retrying; do not repeat blindly." };
      }
    } finally {
      signal?.removeEventListener("abort", cancel);
      if (pending === controller) pending = undefined;
      busy = false;
    }
  }
  return { dispatch, setSession, releaseSession, revoke, close: revoke, getSession: () => sessionId };
}

module.exports = { createComputerController };
