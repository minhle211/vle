import { Cursor } from "@cursor/sdk";

const status = await Cursor.auth.status();
if (status.status === "logged-in") {
  console.log("logged-in");
  process.exit(0);
}

console.log("Opening your browser to sign in to Cursor.");
await Cursor.auth.login({ apiKeyName: "VLe" });

const after = await Cursor.auth.status();
if (after.status !== "logged-in") {
  console.error("Cursor sign-in did not finish.");
  process.exit(1);
}
console.log("logged-in");
