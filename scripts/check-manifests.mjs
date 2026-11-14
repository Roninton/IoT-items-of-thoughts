import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => JSON.parse(readFileSync(join(root, path), "utf8"));
const outer = read("package.json");
const inner = read("dsh-plugin-iot-thinking/package.json");
assert.equal(outer.name, inner.name, "package names drifted");
assert.equal(outer.version, inner.version, "package versions drifted");
assert.deepEqual(outer.peerDependencies, inner.peerDependencies, "peerDependencies drifted");
assert.deepEqual(outer.engines, inner.engines, "engines drifted");
assert.deepEqual(outer.dsh?.client, inner.dsh?.client, "dsh client metadata drifted");
assert.ok(String(outer.dsh?.bundle?.patch).endsWith("dsh-plugin-iot-thinking/cordis.patch.yml"), "outer bundle patch drifted");
assert.equal(inner.dsh?.bundle?.patch, "./cordis.patch.yml", "inner bundle patch drifted");
console.log(`PASS manifests aligned at ${outer.name}@${outer.version}`);
