#!/usr/bin/env node
/**
 * End-to-end WAMP demo client using Wampy.js
 * https://github.com/KSDaemon/wampy.js
 *
 * This script connects TWO WAMP clients to the FastAPI server and
 * demonstrates that messages flow in both directions:
 *
 *   Client A  <-->  FastAPI/WAMP server  <-->  Client B
 *
 * What it exercises:
 *   1. RPC call:       Client A calls server-registered procedures
 *   2. PubSub (C->S):  Client A publishes an event that the server
 *                       handles and re-broadcasts to all sessions
 *   3. PubSub (S->C):  Client B receives the re-broadcast event
 *   4. Server push:     Both clients receive the welcome event on connect
 *
 * Usage:
 *   node client.mjs [ws://localhost:8080/ws]
 */

import { Wampy } from "wampy";
import WebSocket from "ws";

const url = process.argv[2] || "ws://localhost:8080/ws";
const realm = "realm1";

// ── Helpers ──────────────────────────────────────────────────────────

function log(tag, ...args) {
  console.log(`  [${tag}]`, ...args);
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

/** Create and connect a Wampy client. Returns the wampy instance. */
async function openConnection(label) {
  const wampy = new Wampy(url, {
    realm,
    ws: WebSocket,
    autoReconnect: false,
  });

  await wampy.connect();
  log(label, `Connected  (session ${wampy.getSessionId()})`);
  return wampy;
}

// ── Main ─────────────────────────────────────────────────────────────

async function main() {
  console.log();
  console.log("=".repeat(60));
  console.log("  fastapi-headless-wamp  —  End-to-End Demo (Wampy.js)");
  console.log("=".repeat(60));
  console.log();
  console.log(`  Connecting to: ${url}`);
  console.log(`  Realm:         ${realm}`);
  console.log();

  // ── 1. Connect Client B first (subscriber) ──────────────────────
  console.log("─".repeat(60));
  console.log("  Step 1: Connect Client B (subscriber)");
  console.log("─".repeat(60));

  const clientB = await openConnection("Client B");

  // Client B subscribes to the "loud" echo topic (server re-broadcast)
  const loudEvents = [];
  await clientB.subscribe("com.example.echo.loud", (eventData) => {
    log("Client B", `Received loud echo: ${JSON.stringify(eventData.argsList)}`);
    loudEvents.push(eventData);
  });
  log("Client B", "Subscribed to com.example.echo.loud");

  // Client B subscribes to pong events
  const pongEvents = [];
  await clientB.subscribe("com.example.pong", (eventData) => {
    log("Client B", `Received pong: ${JSON.stringify(eventData.argsList)}`);
    pongEvents.push(eventData);
  });
  log("Client B", "Subscribed to com.example.pong");

  // Client B subscribes to welcome events
  const welcomeEvents = [];
  await clientB.subscribe("com.example.welcome", (eventData) => {
    log("Client B", `Welcome event: ${eventData.argsList[0]}`);
    welcomeEvents.push(eventData.argsList[0]);
  });
  log("Client B", "Subscribed to com.example.welcome");

  // Give server time to send the welcome event (server schedules it 0.5s after connect)
  await sleep(1000);

  // ── 2. Connect Client A (caller / publisher) ───────────────────
  console.log();
  console.log("─".repeat(60));
  console.log("  Step 2: Connect Client A (caller / publisher)");
  console.log("─".repeat(60));

  const clientA = await openConnection("Client A");

  // Client A also subscribes to welcome
  await clientA.subscribe("com.example.welcome", (eventData) => {
    log("Client A", `Welcome event: ${eventData.argsList[0]}`);
    welcomeEvents.push(eventData.argsList[0]);
  });

  // Give server time to send welcome for Client A
  await sleep(1000);

  // ── 3. RPC calls from Client A ─────────────────────────────────
  console.log();
  console.log("─".repeat(60));
  console.log("  Step 3: Client A calls server RPCs");
  console.log("─".repeat(60));

  // 3a. com.example.add
  const addResult = await clientA.call("com.example.add", [17, 25]);
  const sum = addResult.argsList[0];
  log("Client A", `com.example.add(17, 25) => ${sum}`);

  // 3b. com.example.greet
  const greetResult = await clientA.call("com.example.greet", ["WAMP"]);
  const greeting = greetResult.argsList[0];
  log("Client A", `com.example.greet("WAMP") => ${greeting}`);

  // 3c. com.example.time
  const timeResult = await clientA.call("com.example.time");
  const time = timeResult.argsList[0];
  log("Client A", `com.example.time() => ${time}`);

  // 3d. com.example.echo.reverse
  const revResult = await clientA.call("com.example.echo.reverse", [
    "fastapi-headless-wamp",
  ]);
  const reversed = revResult.argsList[0];
  log("Client A", `com.example.echo.reverse("fastapi-headless-wamp") => ${reversed}`);

  // 3e. com.example.echo.stats
  const statsResult = await clientA.call("com.example.echo.stats");
  const stats = statsResult.argsList[0];
  log("Client A", `com.example.echo.stats() => ${JSON.stringify(stats)}`);

  // ── 4. PubSub: Client A publishes, Server transforms, Client B receives
  console.log();
  console.log("─".repeat(60));
  console.log("  Step 4: Client A publishes -> Server -> Client B receives");
  console.log("─".repeat(60));

  // 4a. Publish to com.example.echo.shout — server uppercases and
  //     re-publishes to com.example.echo.loud
  await clientA.publish("com.example.echo.shout", ["hello from wampy.js"]);
  log("Client A", 'Published to com.example.echo.shout: "hello from wampy.js"');

  // Wait for server to process and broadcast
  await sleep(500);

  // 4b. Publish a ping — server replies with pong to all
  await clientA.publish("com.example.ping", ["are you there?"]);
  log("Client A", 'Published to com.example.ping: "are you there?"');

  await sleep(500);

  // ── 5. Summary ──────────────────────────────────────────────────
  console.log();
  console.log("─".repeat(60));
  console.log("  Summary");
  console.log("─".repeat(60));
  console.log();

  const checks = [
    ["RPC com.example.add(17, 25) = 42", sum === 42],
    ['RPC com.example.greet("WAMP") = "Hello, WAMP!"', greeting === "Hello, WAMP!"],
    ["RPC com.example.time returned ISO string", typeof time === "string" && time.includes("T")],
    ['RPC com.example.echo.reverse = "pmaw-sseldaeh-ipatsaf"', reversed === "pmaw-sseldaeh-ipatsaf"],
    ["Client B received loud echo event", loudEvents.length > 0],
    ["Client B received pong event", pongEvents.length > 0],
    ["Welcome events received", welcomeEvents.length > 0],
  ];

  let allPassed = true;
  for (const [desc, ok] of checks) {
    const mark = ok ? "PASS" : "FAIL";
    console.log(`  [${mark}] ${desc}`);
    if (!ok) allPassed = false;
  }

  console.log();
  if (allPassed) {
    console.log("  All checks passed! The WAMP integration works end-to-end.");
  } else {
    console.log("  Some checks failed — see output above.");
  }
  console.log();

  // ── Cleanup ─────────────────────────────────────────────────────
  await clientA.disconnect();
  await clientB.disconnect();

  process.exit(allPassed ? 0 : 1);
}

main().catch((err) => {
  console.error("Fatal error:", err);
  process.exit(1);
});
