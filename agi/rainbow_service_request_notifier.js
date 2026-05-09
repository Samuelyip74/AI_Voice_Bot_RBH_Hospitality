#!/usr/bin/env node
"use strict";

const RainbowSDK = require("rainbow-node-sdk");

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", chunk => {
      data += chunk;
    });
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", reject);
  });
}

function requireEnv(name) {
  const value = (process.env[name] || "").trim();
  if (!value) {
    throw new Error(`${name} is required`);
  }
  return value;
}

function optionsFromEnv() {
  return {
    rainbow: {
      host: process.env.RAINBOW_HOST || "official",
      mode: process.env.RAINBOW_MODE || "xmpp",
    },
    credentials: {
      login: requireEnv("RAINBOW_LOGIN"),
      password: requireEnv("RAINBOW_PASSWORD"),
    },
    application: {
      appID: requireEnv("RAINBOW_APP_ID"),
      appSecret: requireEnv("RAINBOW_APP_SECRET"),
    },
    logs: {
      enableConsoleLogs: (process.env.RAINBOW_SDK_CONSOLE_LOGS || "false").toLowerCase() === "true",
      enableFileLogs: false,
      color: false,
      level: process.env.RAINBOW_SDK_LOG_LEVEL || "warn",
      customLabel: "service-request-notifier",
    },
    im: {
      sendReadReceipt: true,
      storeMessages: true,
      sendMessageToConnectedUser: false,
    },
  };
}

function formatMessage(payload) {
  const request = payload.request || {};
  const lines = [
    `New ${payload.destination || "hotel"} request`,
    `Category: ${request.category || "service_request"}`,
    `Room: ${request.room_number || "unknown"}`,
    `Guest: ${request.guest_name || payload.caller_name || "unknown"}`,
    `Priority: ${request.priority || "normal"}`,
    `Summary: ${request.summary || ""}`,
  ];

  if (request.preferred_time) lines.push(`Preferred time: ${request.preferred_time}`);
  if (request.access_permission) lines.push(`Access: ${request.access_permission}`);
  if (request.language || payload.preferred_language) lines.push(`Language: ${request.language || payload.preferred_language}`);
  if (request.notes) lines.push(`Notes: ${request.notes}`);
  lines.push(`Call ID: ${payload.call_id || ""}`);

  return lines.filter(line => !line.endsWith(": ")).join("\n");
}

function waitForReady(sdk, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Timed out waiting for rainbow_onready")), timeoutMs);

    sdk.events.on("rainbow_onready", () => {
      clearTimeout(timer);
      resolve();
    });

    sdk.events.on("rainbow_onconnectionerror", error => {
      clearTimeout(timer);
      reject(new Error(`Rainbow connection error: ${JSON.stringify(error)}`));
    });

    sdk.events.on("rainbow_onfailed", error => {
      clearTimeout(timer);
      reject(new Error(`Rainbow connection failed: ${JSON.stringify(error)}`));
    });

    sdk.events.on("rainbow_onerror", error => {
      clearTimeout(timer);
      reject(new Error(`Rainbow SDK error: ${JSON.stringify(error)}`));
    });
  });
}

async function stopWithTimeout(sdk, timeoutMs) {
  await Promise.race([
    sdk.stop(),
    new Promise(resolve => setTimeout(resolve, timeoutMs)),
  ]).catch(() => {});
}

async function main() {
  const raw = await readStdin();
  const payload = JSON.parse(raw || "{}");
  const bubbleJid = (payload.bubble_jid || "").trim();
  if (!bubbleJid) {
    throw new Error("bubble_jid is required");
  }

  const sdk = new RainbowSDK(optionsFromEnv());
  const readyTimeoutMs = Number(process.env.RAINBOW_NODE_READY_TIMEOUT_MS || 30000);
  const ready = waitForReady(sdk, readyTimeoutMs);

  let started = false;
  const originalStdoutWrite = process.stdout.write.bind(process.stdout);
  process.stdout.write = (chunk, encoding, callback) => {
    const text = Buffer.isBuffer(chunk) ? chunk.toString("utf8") : String(chunk);
    process.stderr.write(text, encoding);
    if (typeof callback === "function") callback();
    return true;
  };
  try {
    await sdk.start();
    started = true;
    await ready;

    const message = formatMessage(payload);
    const sent = await sdk.im.sendMessageToBubbleJid(message, bubbleJid);

    process.stdout.write = originalStdoutWrite;
    process.stdout.write(JSON.stringify({ sent: true, destination: payload.destination, bubble_jid: bubbleJid, message_id: sent && sent.id ? sent.id : null }));
  } finally {
    process.stdout.write = originalStdoutWrite;
    if (started) {
      const stopTimeoutMs = Number(process.env.RAINBOW_NODE_STOP_TIMEOUT_MS || 5000);
      await stopWithTimeout(sdk, stopTimeoutMs);
    }
  }
}

main().catch(error => {
  process.stderr.write(`${error && error.stack ? error.stack : error}\n`);
  process.exit(1);
}).then(() => {
  process.exit(0);
});
