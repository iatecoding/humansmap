// This script runs inside arangosh on first ArangoDB start.
// It is idempotent — safe to run multiple times.
//
// It creates the application database (if missing) and the restricted
// application user, then grants that user read/write on the application
// database only. It never grants access to _system.

const users = require("@arangodb/users");
const db = require("@arangodb").db;
const process = require("process");

const DB_NAME = process.env.ARANGO_DB || "world_graph_all";
const appUser = process.env.ARANGO_APP_USERNAME || "app_user";
const appPassword = process.env.ARANGO_APP_PASSWORD;

if (!appPassword) throw new Error("ARANGO_APP_PASSWORD env var is not set");

// 1. Create the application database if it does not exist yet. Granting
//    collection-level access below requires the database to exist.
if (!db._databases().includes(DB_NAME)) {
  db._createDatabase(DB_NAME);
  print("Created database: " + DB_NAME);
}

// 2. Create the application user (or update its password if it exists).
try {
  users.save(appUser, appPassword);
  print("Created user: " + appUser);
} catch (e) {
  if (e.errorNum === 1702) {
    // User already exists — update password.
    users.replace(appUser, appPassword);
    print("Updated existing user: " + appUser);
  } else {
    throw e;
  }
}

// 3. Grant read/write on the application database only.
users.grantDatabase(appUser, DB_NAME, "rw");
users.grantCollection(appUser, DB_NAME, "*", "rw");

// 4. Explicitly deny access to _system.
users.grantDatabase(appUser, "_system", "none");

print("Permissions configured for " + appUser + " on " + DB_NAME);
