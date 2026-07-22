import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

import { createBroker, protocol } from "../src/index.js";

const contract = JSON.parse(
  readFileSync(new URL("../protocol-contract.json", import.meta.url), "utf8"),
);
const NOW = Date.parse("2026-07-21T12:00:00.000Z");
const NOW_SECONDS = Math.floor(NOW / 1000);
const ACCOUNT_ID = "0123456789abcdef0123456789abcdef";
const ACCESS_KEY = "1".repeat(32);
const SECRET_KEY = "2".repeat(64);
const AUDIENCE = protocol.githubAudience;
const ENDPOINT = `https://${ACCOUNT_ID}.r2.cloudflarestorage.com`;
const BUCKET = "freesense-builds";
const DEPLOYMENT = `${"d".repeat(40)}.123456789.1`;
const KID = "test-key";
const encoder = new TextEncoder();
const decoder = new TextDecoder();

const env = Object.freeze({
  GITHUB_OIDC_AUDIENCE: AUDIENCE,
  R2_ACCOUNT_ID: ACCOUNT_ID,
  R2_ENDPOINT: ENDPOINT,
  R2_BUCKET: BUCKET,
  BROKER_DEPLOYMENT_ID: DEPLOYMENT,
  R2_PARENT_ACCESS_KEY_ID: ACCESS_KEY,
  R2_PARENT_SECRET_ACCESS_KEY: SECRET_KEY,
});

const keyPair = await crypto.subtle.generateKey(
  {
    name: "RSASSA-PKCS1-v1_5",
    modulusLength: 2048,
    publicExponent: new Uint8Array([1, 0, 1]),
    hash: "SHA-256",
  },
  true,
  ["sign", "verify"],
);
const publicJwk = {
  ...(await crypto.subtle.exportKey("jwk", keyPair.publicKey)),
  kid: KID,
  use: "sig",
  alg: "RS256",
};

function binary(bytes) {
  return String.fromCharCode(...bytes);
}

function b64url(bytes) {
  return btoa(binary(bytes))
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/u, "");
}

function decodeB64url(value) {
  const padding = "=".repeat((4 - (value.length % 4)) % 4);
  return Uint8Array.from(
    atob(value.replaceAll("-", "+").replaceAll("_", "/") + padding),
    (character) => character.charCodeAt(0),
  );
}

function segment(value) {
  return b64url(encoder.encode(JSON.stringify(value)));
}

function claimsFor(role, overrides = {}) {
  const common = {
    iss: protocol.githubIssuer,
    aud: AUDIENCE,
    iat: NOW_SECONDS,
    nbf: NOW_SECONDS - 30,
    exp: NOW_SECONDS + 300,
    jti: "11111111-2222-4333-8444-555555555555",
    repository: protocol.githubRepository,
    repository_id: protocol.githubRepositoryId,
    repository_owner_id: protocol.githubRepositoryOwnerId,
    repository_visibility: "public",
    ref: "refs/heads/main",
    ref_type: "branch",
    ref_protected: "true",
    runner_environment: "github-hosted",
    run_id: "123456789",
    run_number: "17",
    run_attempt: "1",
    sha: "a".repeat(40),
    workflow_sha: "b".repeat(40),
    event_name: "schedule",
  };
  const variants = {
    coordinator: {
      environment: "build-coordinator",
      workflow_ref: protocol.workflows.system,
      job_workflow_ref: protocol.workflows.system,
      job_workflow_sha: "b".repeat(40),
    },
    "artifact-writer": {
      environment: "build",
      runner_environment: "self-hosted",
      workflow_ref: protocol.workflows.system,
      job_workflow_ref: protocol.workflows.runnerBuild,
      job_workflow_sha: "b".repeat(40),
    },
    "pin-writer": {
      environment: "pin",
      runner_environment: "self-hosted",
      workflow_ref: protocol.workflows.pin,
      job_workflow_ref: protocol.workflows.pin,
      job_workflow_sha: "b".repeat(40),
    },
    "channel-writer": {
      environment: "channel-publisher",
      workflow_ref: protocol.workflows.packages,
      job_workflow_ref: protocol.workflows.packages,
      job_workflow_sha: "b".repeat(40),
    },
    "broker-smoke": {
      environment: "broker",
      workflow_ref: protocol.workflows.broker,
      job_workflow_ref: protocol.workflows.broker,
      job_workflow_sha: "b".repeat(40),
      event_name: "push",
    },
  };
  const claims = { ...common, ...variants[role], ...overrides };
  claims.sub =
    overrides.sub ??
    `${protocol.githubSubjectPrefix}:environment:${claims.environment}`;
  return claims;
}

async function tokenFor(claims, { kid = KID, key = keyPair.privateKey } = {}) {
  const header = segment({ alg: "RS256", typ: "JWT", kid });
  const payload = segment(claims);
  const input = `${header}.${payload}`;
  const signature = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    key,
    encoder.encode(input),
  );
  return `${input}.${b64url(new Uint8Array(signature))}`;
}

function harness(keys = [publicJwk]) {
  return createBroker({
    now: () => NOW,
    fetch: async (url) => {
      assert.equal(url, protocol.githubJwksUrl);
      return new Response(JSON.stringify({ keys }), {
        headers: { "Content-Type": "application/json" },
      });
    },
  });
}

async function request(role, claims = claimsFor(role), tokenOptions) {
  const bearer = await tokenFor(claims, tokenOptions);
  return harness().fetch(
    new Request("https://broker.example/v1/credentials", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${bearer}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        schema_version: protocol.requestSchema,
        role,
      }),
    }),
    env,
  );
}

function decodeSession(value) {
  const encoded = atob(value);
  assert.match(encoded, /^jwt\//u);
  const parts = encoded.slice(4).split(".");
  assert.equal(parts.length, 3);
  return JSON.parse(decoder.decode(decodeB64url(parts[1])));
}

describe("configuration and protocol", () => {
  it("exposes health only for a complete deployment", async () => {
    const broker = harness();
    const healthy = await broker.fetch(
      new Request(`https://broker.example/healthz?deployment_id=${DEPLOYMENT}`),
      env,
    );
    assert.equal(healthy.status, 200);
    assert.deepEqual(await healthy.json(), {
      status: "ok",
      deployment_id: DEPLOYMENT,
    });

    const incomplete = await broker.fetch(
      new Request("https://broker.example/healthz"),
      { ...env, R2_BUCKET: "" },
    );
    assert.equal(incomplete.status, 503);
  });

  it("keeps the checked-in protocol synchronized", () => {
    assert.equal(contract.prefix, protocol.r2Prefix);
    assert.deepEqual(contract.roles, Object.keys(protocol.roles).sort());
    assert.equal(contract.request_schema, protocol.requestSchema);
    assert.equal(contract.response_schema, protocol.responseSchema);
  });
});

describe("least-privilege role policies", () => {
  const cases = [
    ["coordinator", 2700, ["v1/state/generations/"], []],
    [
      "artifact-writer",
      20700,
      ["v1/inputs/sha256/", "v1/artifacts/"],
      [],
    ],
    ["pin-writer", 20700, ["v1/inputs/sha256/"], []],
    [
      "channel-writer",
      4500,
      [],
      [
        "v1/repos.manifest.json",
        "v1/releases/stable.json",
        "v1/releases/devel.json",
      ],
    ],
    ["broker-smoke", 900, [], [`v1/smoke/broker/${"a".repeat(40)}.json`]],
  ];

  for (const [role, ttl, prefixes, objects] of cases) {
    it(`issues only the ${role} scope`, async () => {
      const response = await request(role);
      assert.equal(response.status, 200);
      const body = await response.json();
      assert.equal(body.schema_version, protocol.responseSchema);
      assert.equal(body.prefix, "v1");
      assert.equal(body.bucket, BUCKET);
      assert.equal(body.endpoint, ENDPOINT);
      const session = decodeSession(body.session_token);
      assert.deepEqual(session.paths.prefixPaths, prefixes);
      assert.deepEqual(session.paths.objectPaths, objects);
      assert.equal(session.exp - session.iat, ttl);
      assert.ok(session.actions.includes("PutObject"));
      assert.equal(
        session.actions.includes("ListObjectsV2"),
        role === "artifact-writer",
      );
      assert.ok(!session.actions.includes("DeleteObject"));
      assert.ok(!session.actions.some((action) => /Multipart/u.test(action)));
    });
  }
});

describe("automatic package-chain identity", () => {
  const packageEntry = {
    workflow_ref: protocol.workflows.packages,
    job_workflow_ref: protocol.workflows.packages,
    event_name: "workflow_run",
  };

  for (const role of ["coordinator", "channel-writer"]) {
    it(`allows ${role} only for the direct package workflow_run`, async () => {
      const response = await request(role, claimsFor(role, packageEntry));
      assert.equal(response.status, 200);
    });
  }

  it("allows the package workflow_run to call the reusable artifact writer", async () => {
    const response = await request(
      "artifact-writer",
      claimsFor("artifact-writer", {
        workflow_ref: protocol.workflows.packages,
        event_name: "workflow_run",
      }),
    );
    assert.equal(response.status, 200);
  });

  for (const role of ["coordinator", "artifact-writer", "channel-writer"]) {
    it(`rejects ${role} workflow_run access from the System workflow`, async () => {
      const response = await request(
        role,
        claimsFor(role, {
          workflow_ref: protocol.workflows.system,
          job_workflow_ref:
            role === "artifact-writer"
              ? protocol.workflows.runnerBuild
              : protocol.workflows.system,
          event_name: "workflow_run",
        }),
      );
      assert.equal(response.status, 403);
      assert.deepEqual(await response.json(), { error: "access_denied" });
    });
  }
});

describe("stable patch workflow identity", () => {
  for (const role of ["coordinator", "channel-writer"]) {
    it(`allows ${role} for a direct stable workflow dispatch`, async () => {
      const response = await request(
        role,
        claimsFor(role, {
          workflow_ref: protocol.workflows.stable,
          job_workflow_ref: protocol.workflows.stable,
          event_name: "workflow_dispatch",
        }),
      );
      assert.equal(response.status, 200);
    });
  }

  it("allows stable to call the reusable artifact writer", async () => {
    const response = await request(
      "artifact-writer",
      claimsFor("artifact-writer", {
        workflow_ref: protocol.workflows.stable,
        event_name: "workflow_dispatch",
      }),
    );
    assert.equal(response.status, 200);
  });
});

describe("identity boundaries", () => {
  const rejected = [
    ["wrong repository id", { repository_id: "9" }],
    ["unprotected ref", { ref_protected: "false" }],
    ["wrong branch", { ref: "refs/heads/feature" }],
    ["wrong environment", { environment: "production" }],
    ["wrong runner", { runner_environment: "self-hosted" }],
    ["wrong workflow", { workflow_ref: protocol.workflows.broker }],
  ];

  for (const [name, overrides] of rejected) {
    it(`rejects ${name}`, async () => {
      const response = await request(
        "coordinator",
        claimsFor("coordinator", overrides),
      );
      assert.equal(response.status, 403);
      assert.deepEqual(await response.json(), { error: "access_denied" });
    });
  }

  it("rejects an unknown signing key", async () => {
    const response = await request("coordinator", claimsFor("coordinator"), {
      kid: "unknown",
    });
    assert.equal(response.status, 401);
  });

  it("rejects an invalid signature", async () => {
    const other = await crypto.subtle.generateKey(
      {
        name: "RSASSA-PKCS1-v1_5",
        modulusLength: 2048,
        publicExponent: new Uint8Array([1, 0, 1]),
        hash: "SHA-256",
      },
      true,
      ["sign", "verify"],
    );
    const response = await request("coordinator", claimsFor("coordinator"), {
      key: other.privateKey,
    });
    assert.equal(response.status, 401);
  });
});
