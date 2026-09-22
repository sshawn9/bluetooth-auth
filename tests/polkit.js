const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(process.argv[2], "utf8");
let rule;
let spawnCalls = [];
let spawnFails = false;
const context = {
  polkit: {
    Result: { YES: "yes", NOT_HANDLED: "not-handled" },
    addRule(candidate) {
      rule = candidate;
    },
    spawn(argv) {
      spawnCalls.push(argv);
      if (spawnFails) throw new Error("BLE unavailable");
    },
  },
};
vm.runInNewContext(source, context);
if (!rule) throw new Error("polkit rule was not registered");

function check(condition, message) {
  if (!condition) throw new Error(message);
}

function evaluate(subject, actionId, fails = false) {
  spawnCalls = [];
  spawnFails = fails;
  return rule({ id: actionId }, subject);
}

const trusted = { user: "nobody", local: true, active: true };
for (const action of [
  "org.freedesktop.systemd1.reload-daemon",
  "org.freedesktop.policykit.exec",
  "org.gtk.vfs.file-operations",
  "test.other-action",
]) {
  check(evaluate(trusted, action) === "yes", `${action} was not authorized`);
  check(spawnCalls.length === 1, "eligible request did not invoke helper");
  check(
    spawnCalls[0].length === 5 &&
      spawnCalls[0][1] === "--address-file" &&
      spawnCalls[0][2] === "/private/test-address" &&
      spawnCalls[0][3] === "--connect" &&
      spawnCalls[0][4] === "-1",
    "helper arguments differ from the connect-notification contract",
  );
  check(
    evaluate(trusted, action, true) === "not-handled",
    `${action} helper error bypassed fallback`,
  );
  check(spawnCalls.length === 1, "failing eligible helper was not called");
}

for (const subject of [
  { user: "other", local: true, active: true },
  { user: "nobody", local: false, active: true },
  { user: "nobody", local: true, active: false },
]) {
  check(
    evaluate(subject, "org.gtk.vfs.file-operations") === "not-handled",
    "ineligible request was handled",
  );
  check(spawnCalls.length === 0, "ineligible request invoked helper");
}
