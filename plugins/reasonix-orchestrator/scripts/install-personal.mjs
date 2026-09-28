#!/usr/bin/env node

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const PLUGIN_NAME = "reasonix-orchestrator";
const args = process.argv.slice(2);
const replace = args.includes("--replace");
const openAfterInstall = args.includes("--open");
const homeIndex = args.indexOf("--home");

if (args.some((arg, index) =>
  !["--replace", "--open", "--home"].includes(arg)
  && index !== homeIndex + 1
)) {
  console.error("Usage: node scripts/install-personal.mjs [--replace] [--open] [--home PATH]");
  process.exit(2);
}

if (homeIndex !== -1 && !args[homeIndex + 1]) {
  console.error("--home requires a path");
  process.exit(2);
}

const home = path.resolve(homeIndex === -1 ? os.homedir() : args[homeIndex + 1]);
const scriptPath = fileURLToPath(import.meta.url);
const sourceRoot = path.resolve(path.dirname(scriptPath), "..");
const pluginsRoot = path.join(home, "plugins");
const destination = path.join(pluginsRoot, PLUGIN_NAME);
const marketplacePath = path.join(home, ".agents", "plugins", "marketplace.json");
const marketplaceDirectory = path.dirname(marketplacePath);

const manifestPath = path.join(sourceRoot, ".codex-plugin", "plugin.json");
if (!fs.existsSync(manifestPath)) {
  console.error(`Plugin manifest not found: ${manifestPath}`);
  process.exit(1);
}

let manifest;
try {
  manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
} catch (error) {
  console.error(`Plugin manifest is invalid JSON: ${error.message}`);
  process.exit(1);
}
if (manifest.name !== PLUGIN_NAME) {
  console.error(`Plugin manifest name must be ${PLUGIN_NAME}`);
  process.exit(1);
}

let marketplace = {
  name: "personal",
  interface: { displayName: "Personal" },
  plugins: [],
};
if (fs.existsSync(marketplacePath)) {
  try {
    marketplace = JSON.parse(fs.readFileSync(marketplacePath, "utf8"));
  } catch (error) {
    console.error(`Existing marketplace is invalid JSON; no changes made: ${error.message}`);
    process.exit(1);
  }
  if (marketplace.name !== "personal" || !Array.isArray(marketplace.plugins)) {
    console.error("Existing personal marketplace has an unsupported shape; no changes made.");
    process.exit(1);
  }
  if (!marketplace.interface || typeof marketplace.interface !== "object") {
    marketplace.interface = { displayName: "Personal" };
  }
}

if (fs.existsSync(destination) && path.resolve(destination) !== sourceRoot && !replace) {
  console.error(`Destination already exists: ${destination}`);
  console.error("Re-run with --replace to update it while keeping a timestamped backup.");
  process.exit(1);
}

fs.mkdirSync(pluginsRoot, { recursive: true });
fs.mkdirSync(marketplaceDirectory, { recursive: true });

let backup = null;
if (path.resolve(destination) !== sourceRoot) {
  if (fs.existsSync(destination)) {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    backup = `${destination}.backup-${stamp}`;
    fs.renameSync(destination, backup);
  }

  const staging = path.join(pluginsRoot, `.${PLUGIN_NAME}.install-${process.pid}`);
  try {
    fs.cpSync(sourceRoot, staging, {
      recursive: true,
      errorOnExist: true,
      filter: (entry) => path.basename(entry) !== ".git",
    });
    fs.renameSync(staging, destination);
  } catch (error) {
    if (fs.existsSync(staging)) {
      fs.rmSync(staging, { recursive: true, force: true });
    }
    if (backup && !fs.existsSync(destination)) {
      fs.renameSync(backup, destination);
    }
    console.error(`Plugin copy failed: ${error.message}`);
    process.exit(1);
  }
}

const entry = {
  name: PLUGIN_NAME,
  source: { source: "local", path: `./plugins/${PLUGIN_NAME}` },
  policy: { installation: "AVAILABLE", authentication: "ON_INSTALL" },
  category: "Productivity",
};
const existingIndex = marketplace.plugins.findIndex((plugin) => plugin?.name === PLUGIN_NAME);
if (existingIndex === -1) {
  marketplace.plugins.push(entry);
} else {
  marketplace.plugins[existingIndex] = entry;
}

const temporaryMarketplace = `${marketplacePath}.tmp-${process.pid}`;
try {
  fs.writeFileSync(temporaryMarketplace, `${JSON.stringify(marketplace, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
    flag: "wx",
  });
  fs.renameSync(temporaryMarketplace, marketplacePath);
} catch (error) {
  if (fs.existsSync(temporaryMarketplace)) {
    fs.rmSync(temporaryMarketplace, { force: true });
  }
  console.error(`Marketplace update failed: ${error.message}`);
  process.exit(1);
}

const encodedMarketplace = encodeURIComponent(marketplacePath);
const deepLink = `codex://plugins/${PLUGIN_NAME}?marketplacePath=${encodedMarketplace}`;

console.log(`Installed plugin: ${destination}`);
console.log(`Personal marketplace: ${marketplacePath}`);
if (backup) {
  console.log(`Previous installation backed up to: ${backup}`);
}
console.log(`Open in Codex: ${deepLink}`);
console.log("After installation in Codex, start a new conversation before invoking the skill.");

if (openAfterInstall) {
  if (process.platform !== "darwin") {
    console.error("--open is supported only on macOS; use the printed link.");
    process.exit(2);
  }
  execFileSync("open", [deepLink], { stdio: "inherit" });
}
