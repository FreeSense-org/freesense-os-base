const GITHUB_ISSUER = "https://token.actions.githubusercontent.com";
const GITHUB_JWKS_URL =
  "https://token.actions.githubusercontent.com/.well-known/jwks";
const GITHUB_REPOSITORY = "FreeSense-org/freesense-os-base";
const GITHUB_REPOSITORY_ID = "1307808114";
const GITHUB_REPOSITORY_OWNER_ID = "297040764";
const GITHUB_SUBJECT_PREFIX =
  "repo:FreeSense-org@297040764/freesense-os-base@1307808114";
const GITHUB_AUDIENCE = "https://r2-credentials.freesense.org";
const MAIN_REF = "refs/heads/main";
const R2_PREFIX = "v1";
const REGION = "auto";

const SYSTEM_WORKFLOW =
  `${GITHUB_REPOSITORY}/.github/workflows/system.yml@${MAIN_REF}`;
const PACKAGES_WORKFLOW =
  `${GITHUB_REPOSITORY}/.github/workflows/packages.yml@${MAIN_REF}`;
const RYZEN_BUILD_WORKFLOW =
  `${GITHUB_REPOSITORY}/.github/workflows/ryzen-build.yml@${MAIN_REF}`;
const BUILD_ENTRY_WORKFLOWS = Object.freeze([
  SYSTEM_WORKFLOW,
  PACKAGES_WORKFLOW,
  `${GITHUB_REPOSITORY}/.github/workflows/release.yml@${MAIN_REF}`,
]);
const PIN_WORKFLOW =
  `${GITHUB_REPOSITORY}/.github/workflows/pin.yml@${MAIN_REF}`;
const RELEASE_WORKFLOW =
  `${GITHUB_REPOSITORY}/.github/workflows/release.yml@${MAIN_REF}`;
const BROKER_WORKFLOW =
  `${GITHUB_REPOSITORY}/.github/workflows/broker.yml@${MAIN_REF}`;

const REQUEST_SCHEMA = "fsbuild.credential-request/v1";
const RESPONSE_SCHEMA = "fsbuild.temporary-r2-credentials/v1";
const CLOCK_TOLERANCE_SECONDS = 30;
const MAX_GITHUB_TOKEN_AGE_SECONDS = 10 * 60;
const JWKS_CACHE_MILLISECONDS = 60 * 1000;
const MAX_REQUEST_BYTES = 1024;
const MAX_TOKEN_BYTES = 16 * 1024;
const SHA_PATTERN = /^[0-9a-f]{40}$/;
const RUN_ID_PATTERN = /^[1-9][0-9]{0,19}$/;
const DEPLOYMENT_ID_PATTERN =
  /^[0-9a-f]{40}\.[1-9][0-9]{0,19}\.[1-9][0-9]{0,9}$/u;
const ROLE_DEFINITIONS = Object.freeze({
  coordinator: {
    environments: ["build-coordinator"],
    workflow: "entry",
    actions: ["GetObject", "HeadObject", "PutObject"],
    ttlSeconds: 45 * 60,
    paths() {
      return [`${R2_PREFIX}/state/generations/`];
    },
  },
  "artifact-writer": {
    environments: ["build"],
    workflow: "artifact",
    actions: ["GetObject", "HeadObject", "PutObject"],
    ttlSeconds: 345 * 60,
    paths() {
      return [
        `${R2_PREFIX}/inputs/sha256/`,
        `${R2_PREFIX}/artifacts/`,
      ];
    },
  },
  "pin-writer": {
    environments: ["pin"],
    workflow: "pin",
    actions: ["GetObject", "HeadObject", "PutObject"],
    ttlSeconds: 345 * 60,
    paths() {
      return [`${R2_PREFIX}/inputs/sha256/`];
    },
  },
  "channel-writer": {
    environments: ["channel-publisher"],
    workflow: "entry",
    actions: ["GetObject", "HeadObject", "PutObject"],
    ttlSeconds: 75 * 60,
    pathKind: "object",
    paths() {
      return [`${R2_PREFIX}/repos.manifest.json`];
    },
  },
  "broker-smoke": {
    environments: ["broker"],
    workflow: "broker",
    actions: ["HeadObject", "PutObject"],
    ttlSeconds: 15 * 60,
    pathKind: "object",
    paths(claims) {
      return [
        `${R2_PREFIX}/smoke/broker/${claims.sha}.json`,
      ];
    },
  },
});

const REQUIRED_CONFIGURATION = Object.freeze([
  "GITHUB_OIDC_AUDIENCE",
  "R2_ACCOUNT_ID",
  "R2_ENDPOINT",
  "R2_BUCKET",
  "BROKER_DEPLOYMENT_ID",
  "R2_PARENT_ACCESS_KEY_ID",
  "R2_PARENT_SECRET_ACCESS_KEY",
]);

const encoder = new TextEncoder();

class BrokerError extends Error {
  constructor(status, code) {
    super(code);
    this.status = status;
    this.code = code;
  }
}

function jsonResponse(value, status = 200, extraHeaders = undefined) {
  const headers = new Headers(extraHeaders);
  headers.set("Cache-Control", "no-store");
  headers.set("Content-Type", "application/json; charset=utf-8");
  headers.set("X-Content-Type-Options", "nosniff");
  return new Response(`${JSON.stringify(value)}\n`, { status, headers });
}

function errorResponse(error) {
  if (error instanceof BrokerError) {
    return jsonResponse({ error: error.code }, error.status);
  }
  return jsonResponse({ error: "internal_error" }, 500);
}

function bytesToBinary(bytes) {
  let result = "";
  for (const byte of bytes) {
    result += String.fromCharCode(byte);
  }
  return result;
}

function encodeBase64Url(bytes) {
  return btoa(bytesToBinary(bytes))
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/u, "");
}

function decodeBase64Url(value) {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    !/^[A-Za-z0-9_-]+$/u.test(value) ||
    value.length % 4 === 1
  ) {
    throw new BrokerError(401, "invalid_token");
  }
  const padding = "=".repeat((4 - (value.length % 4)) % 4);
  let decoded;
  try {
    decoded = atob(
      value.replaceAll("-", "+").replaceAll("_", "/") + padding,
    );
  } catch {
    throw new BrokerError(401, "invalid_token");
  }
  return Uint8Array.from(decoded, (character) => character.charCodeAt(0));
}

function decodeJsonSegment(value) {
  let parsed;
  try {
    parsed = JSON.parse(new TextDecoder().decode(decodeBase64Url(value)));
  } catch (error) {
    if (error instanceof BrokerError) {
      throw error;
    }
    throw new BrokerError(401, "invalid_token");
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new BrokerError(401, "invalid_token");
  }
  return parsed;
}

function isNonemptyString(value) {
  return typeof value === "string" && value.trim() !== "";
}

function readConfiguration(env) {
  if (
    env === null ||
    typeof env !== "object" ||
    REQUIRED_CONFIGURATION.some((key) => !isNonemptyString(env[key]))
  ) {
    throw new BrokerError(503, "broker_unavailable");
  }

  if (!/^[0-9a-f]{32}$/u.test(env.R2_ACCOUNT_ID)) {
    throw new BrokerError(503, "broker_unavailable");
  }
  if (!DEPLOYMENT_ID_PATTERN.test(env.BROKER_DEPLOYMENT_ID)) {
    throw new BrokerError(503, "broker_unavailable");
  }
  if (
    !/^[0-9a-f]{32}$/u.test(env.R2_PARENT_ACCESS_KEY_ID) ||
    !/^[0-9a-f]{64}$/u.test(env.R2_PARENT_SECRET_ACCESS_KEY)
  ) {
    throw new BrokerError(503, "broker_unavailable");
  }
  if (
    !/^[A-Za-z0-9][A-Za-z0-9._-]{1,126}[A-Za-z0-9]$/u.test(
      env.R2_BUCKET,
    )
  ) {
    throw new BrokerError(503, "broker_unavailable");
  }
  let endpoint;
  try {
    endpoint = new URL(env.R2_ENDPOINT);
  } catch {
    throw new BrokerError(503, "broker_unavailable");
  }
  if (
    env.GITHUB_OIDC_AUDIENCE !== GITHUB_AUDIENCE ||
    endpoint.protocol !== "https:" ||
    endpoint.username !== "" ||
    endpoint.password !== "" ||
    endpoint.pathname !== "/" ||
    endpoint.search !== "" ||
    endpoint.hash !== "" ||
    endpoint.origin !== env.R2_ENDPOINT ||
    endpoint.hostname !==
      `${env.R2_ACCOUNT_ID}.r2.cloudflarestorage.com`
  ) {
    throw new BrokerError(503, "broker_unavailable");
  }

  return {
    audience: GITHUB_AUDIENCE,
    accountId: env.R2_ACCOUNT_ID,
    endpoint: env.R2_ENDPOINT,
    endpointHost: endpoint.host,
    bucket: env.R2_BUCKET,
    deploymentId: env.BROKER_DEPLOYMENT_ID,
    parent: {
      accessKeyId: env.R2_PARENT_ACCESS_KEY_ID,
      secretAccessKey: env.R2_PARENT_SECRET_ACCESS_KEY,
    },
  };
}

async function parseCredentialRequest(request) {
  const contentType = request.headers.get("Content-Type") ?? "";
  if (contentType.split(";", 1)[0].trim().toLowerCase() !== "application/json") {
    throw new BrokerError(415, "unsupported_media_type");
  }
  const contentLength = request.headers.get("Content-Length");
  if (
    contentLength !== null &&
    (!/^[0-9]+$/u.test(contentLength) ||
      Number(contentLength) > MAX_REQUEST_BYTES)
  ) {
    throw new BrokerError(413, "request_too_large");
  }

  if (request.body === null) {
    throw new BrokerError(400, "invalid_request");
  }
  const reader = request.body.getReader();
  const chunks = [];
  let size = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    size += value.byteLength;
    if (size > MAX_REQUEST_BYTES) {
      await reader.cancel();
      throw new BrokerError(413, "request_too_large");
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  let body;
  try {
    body = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new BrokerError(400, "invalid_request");
  }

  let value;
  try {
    value = JSON.parse(body);
  } catch {
    throw new BrokerError(400, "invalid_request");
  }
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new BrokerError(400, "invalid_request");
  }
  const keys = Object.keys(value).sort();
  if (
    keys.length !== 2 ||
    keys[0] !== "role" ||
    keys[1] !== "schema_version" ||
    value.schema_version !== REQUEST_SCHEMA ||
    typeof value.role !== "string" ||
    !Object.hasOwn(ROLE_DEFINITIONS, value.role)
  ) {
    throw new BrokerError(400, "invalid_request");
  }
  return value;
}

function bearerToken(request) {
  const authorization = request.headers.get("Authorization");
  if (authorization === null || !authorization.startsWith("Bearer ")) {
    throw new BrokerError(401, "invalid_token");
  }
  const token = authorization.slice("Bearer ".length);
  if (
    token.length === 0 ||
    token.length > MAX_TOKEN_BYTES ||
    token.includes(" ")
  ) {
    throw new BrokerError(401, "invalid_token");
  }
  return token;
}

function validJwks(value) {
  return (
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    Array.isArray(value.keys) &&
    value.keys.length > 0 &&
    value.keys.length <= 32
  );
}

function githubKey(keys, kid) {
  const matches = keys.filter(
    (key) =>
      key !== null &&
      typeof key === "object" &&
      !Array.isArray(key) &&
      key.kid === kid &&
      key.kty === "RSA" &&
      key.use === "sig" &&
      key.alg === "RS256" &&
      isNonemptyString(key.n) &&
      isNonemptyString(key.e),
  );
  if (matches.length !== 1) {
    return undefined;
  }
  return matches[0];
}

function integerClaim(payload, name) {
  const value = payload[name];
  if (!Number.isSafeInteger(value)) {
    throw new BrokerError(401, "invalid_token");
  }
  return value;
}

function validateStandardClaims(payload, audience, nowSeconds) {
  const issuedAt = integerClaim(payload, "iat");
  const notBefore = integerClaim(payload, "nbf");
  const expiresAt = integerClaim(payload, "exp");
  if (
    payload.iss !== GITHUB_ISSUER ||
    payload.aud !== audience ||
    !isNonemptyString(payload.jti) ||
    notBefore > nowSeconds + CLOCK_TOLERANCE_SECONDS ||
    expiresAt <= nowSeconds - CLOCK_TOLERANCE_SECONDS ||
    issuedAt > nowSeconds + CLOCK_TOLERANCE_SECONDS ||
    issuedAt < nowSeconds - MAX_GITHUB_TOKEN_AGE_SECONDS ||
    expiresAt <= issuedAt
  ) {
    throw new BrokerError(401, "invalid_token");
  }
}

function directJobWorkflow(claims, workflow) {
  const noJobWorkflow =
    (claims.job_workflow_ref === undefined ||
      claims.job_workflow_ref === "") &&
    (claims.job_workflow_sha === undefined ||
      claims.job_workflow_sha === "");
  const matchingJobWorkflow =
    claims.job_workflow_ref === workflow &&
    claims.job_workflow_sha === claims.workflow_sha &&
    SHA_PATTERN.test(claims.job_workflow_sha ?? "");
  return noJobWorkflow || matchingJobWorkflow;
}

function directWorkflow(claims, workflow, events) {
  return (
    claims.workflow_ref === workflow &&
    directJobWorkflow(claims, workflow) &&
    events.includes(claims.event_name)
  );
}

function entryWorkflow(claims) {
  return (
    BUILD_ENTRY_WORKFLOWS.includes(claims.workflow_ref) &&
    directJobWorkflow(claims, claims.workflow_ref) &&
    ["workflow_dispatch", "schedule"].includes(claims.event_name)
  );
}

function artifactWorkflow(claims) {
  return (
    BUILD_ENTRY_WORKFLOWS.includes(claims.workflow_ref) &&
    claims.job_workflow_ref === RYZEN_BUILD_WORKFLOW &&
    SHA_PATTERN.test(claims.job_workflow_sha ?? "") &&
    claims.job_workflow_sha === claims.workflow_sha &&
    ["workflow_dispatch", "schedule"].includes(claims.event_name)
  );
}

function authorizedWorkflow(claims, kind) {
  switch (kind) {
    case "entry":
      return entryWorkflow(claims);
    case "artifact":
      return artifactWorkflow(claims);
    case "pin":
      return directWorkflow(claims, PIN_WORKFLOW, [
        "workflow_dispatch",
        "schedule",
      ]);
    case "release":
      return directWorkflow(claims, RELEASE_WORKFLOW, [
        "workflow_dispatch",
        "schedule",
      ]);
    case "broker":
      return directWorkflow(claims, BROKER_WORKFLOW, [
        "push",
        "workflow_dispatch",
      ]);
    default:
      return false;
  }
}

function authorizeRole(claims, role) {
  const definition = ROLE_DEFINITIONS[role];
  const expectedRunner = ["artifact-writer", "pin-writer"].includes(role)
    ? "self-hosted"
    : "github-hosted";
  if (
    claims.repository !== GITHUB_REPOSITORY ||
    claims.repository_id !== GITHUB_REPOSITORY_ID ||
    claims.repository_owner_id !== GITHUB_REPOSITORY_OWNER_ID ||
    claims.repository_visibility !== "public" ||
    claims.ref !== MAIN_REF ||
    claims.ref_type !== "branch" ||
    claims.ref_protected !== "true" ||
    claims.runner_environment !== expectedRunner ||
    !RUN_ID_PATTERN.test(claims.run_id ?? "") ||
    !SHA_PATTERN.test(claims.sha ?? "") ||
    !SHA_PATTERN.test(claims.workflow_sha ?? "") ||
    !definition.environments.includes(claims.environment) ||
    claims.sub !== `${GITHUB_SUBJECT_PREFIX}:environment:${claims.environment}` ||
    !authorizedWorkflow(claims, definition.workflow)
  ) {
    throw new BrokerError(403, "access_denied");
  }

  const paths = definition.paths(claims);
  return {
    actions: [...definition.actions],
    prefixPaths: definition.pathKind === "object" ? [] : paths,
    objectPaths: definition.pathKind === "object" ? paths : [],
    ttlSeconds: definition.ttlSeconds,
  };
}

async function importGithubKey(jwk) {
  try {
    return await crypto.subtle.importKey(
      "jwk",
      jwk,
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
      false,
      ["verify"],
    );
  } catch {
    throw new BrokerError(401, "invalid_token");
  }
}

function createGithubVerifier(fetchImpl, now) {
  let cachedKeys;
  let cacheExpiresAt = 0;
  let pendingFetch;

  async function fetchKeys(force) {
    const currentTime = now();
    // A fresh cache also rate-limits attacker-controlled unknown key IDs.
    // A genuine GitHub key rotation waits at most this short cache interval.
    if (cachedKeys !== undefined && currentTime < cacheExpiresAt) {
      return cachedKeys;
    }
    if (!force && pendingFetch !== undefined) {
      return pendingFetch;
    }

    const operation = (async () => {
      let response;
      try {
        response = await fetchImpl(GITHUB_JWKS_URL, {
          headers: { Accept: "application/json" },
        });
      } catch {
        throw new BrokerError(503, "identity_provider_unavailable");
      }
      if (!response.ok) {
        throw new BrokerError(503, "identity_provider_unavailable");
      }
      let body;
      try {
        body = await response.json();
      } catch {
        throw new BrokerError(503, "identity_provider_unavailable");
      }
      if (!validJwks(body)) {
        throw new BrokerError(503, "identity_provider_unavailable");
      }
      cachedKeys = body.keys;
      cacheExpiresAt = now() + JWKS_CACHE_MILLISECONDS;
      return cachedKeys;
    })();

    if (!force) {
      pendingFetch = operation;
    }
    try {
      return await operation;
    } finally {
      if (pendingFetch === operation) {
        pendingFetch = undefined;
      }
    }
  }

  return async function verifyGithubToken(token, audience) {
    const segments = token.split(".");
    if (segments.length !== 3) {
      throw new BrokerError(401, "invalid_token");
    }
    const header = decodeJsonSegment(segments[0]);
    const payload = decodeJsonSegment(segments[1]);
    if (
      header.alg !== "RS256" ||
      header.typ !== "JWT" ||
      !isNonemptyString(header.kid)
    ) {
      throw new BrokerError(401, "invalid_token");
    }

    let keys = await fetchKeys(false);
    let jwk = githubKey(keys, header.kid);
    if (jwk === undefined) {
      keys = await fetchKeys(true);
      jwk = githubKey(keys, header.kid);
    }
    if (jwk === undefined) {
      throw new BrokerError(401, "invalid_token");
    }

    const key = await importGithubKey(jwk);
    let verified;
    try {
      verified = await crypto.subtle.verify(
        "RSASSA-PKCS1-v1_5",
        key,
        decodeBase64Url(segments[2]),
        encoder.encode(`${segments[0]}.${segments[1]}`),
      );
    } catch {
      throw new BrokerError(401, "invalid_token");
    }
    if (!verified) {
      throw new BrokerError(401, "invalid_token");
    }

    validateStandardClaims(
      payload,
      audience,
      Math.floor(now() / 1000),
    );
    return payload;
  };
}

async function hmacSha256(keyText, value) {
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(keyText),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return new Uint8Array(
    await crypto.subtle.sign("HMAC", key, encoder.encode(value)),
  );
}

function hexadecimal(bytes) {
  return [...bytes]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

async function createR2Credentials(configuration, policy, now) {
  const nowSeconds = Math.floor(now() / 1000);
  const expiresAt = nowSeconds + policy.ttlSeconds;
  const parent = configuration.parent;
  const header = {
    alg: "HS256",
    typ: "JWT",
  };
  const payload = {
    bucket: configuration.bucket,
    actions: policy.actions,
    paths: {
      prefixPaths: policy.prefixPaths,
      objectPaths: policy.objectPaths,
    },
    sub: configuration.accountId,
    iss: parent.accessKeyId,
    aud: configuration.endpointHost,
    iat: nowSeconds,
    exp: expiresAt,
  };
  const encodedHeader = encodeBase64Url(
    encoder.encode(JSON.stringify(header)),
  );
  const encodedPayload = encodeBase64Url(
    encoder.encode(JSON.stringify(payload)),
  );
  const signingInput = `${encodedHeader}.${encodedPayload}`;
  const signature = await hmacSha256(
    parent.secretAccessKey,
    signingInput,
  );
  const jwt = `${signingInput}.${encodeBase64Url(signature)}`;
  const digest = new Uint8Array(
    await crypto.subtle.digest("SHA-256", encoder.encode(jwt)),
  );

  return {
    schema_version: RESPONSE_SCHEMA,
    access_key_id: parent.accessKeyId,
    secret_access_key: hexadecimal(digest),
    session_token: btoa(`jwt/${jwt}`),
    endpoint: configuration.endpoint,
    region: REGION,
    bucket: configuration.bucket,
    prefix: R2_PREFIX,
    expires_at: new Date(expiresAt * 1000).toISOString(),
  };
}

export function createBroker(options = {}) {
  const fetchImpl = options.fetch ?? globalThis.fetch;
  const now = options.now ?? Date.now;
  if (typeof fetchImpl !== "function" || typeof now !== "function") {
    throw new TypeError("fetch and now must be functions");
  }
  const verifyGithubToken = createGithubVerifier(fetchImpl, now);

  return {
    async fetch(request, env) {
      const url = new URL(request.url);

      if (url.pathname === "/healthz") {
        const requestedDeployment = url.searchParams.get("deployment_id");
        const validHealthQuery =
          url.search === "" ||
          (
            [...url.searchParams.keys()].length === 1 &&
            url.searchParams.has("deployment_id") &&
            isNonemptyString(requestedDeployment)
          );
        if (!validHealthQuery) {
          return jsonResponse({ error: "not_found" }, 404);
        }
        if (request.method !== "GET") {
          return jsonResponse(
            { error: "method_not_allowed" },
            405,
            { Allow: "GET" },
          );
        }
        try {
          const configuration = readConfiguration(env);
          if (
            requestedDeployment !== null &&
            requestedDeployment !== configuration.deploymentId
          ) {
            return jsonResponse({ status: "unavailable" }, 503);
          }
          return jsonResponse({
            status: "ok",
            deployment_id: configuration.deploymentId,
          });
        } catch {
          return jsonResponse({ status: "unavailable" }, 503);
        }
      }

      if (url.pathname !== "/v1/credentials" || url.search !== "") {
        return jsonResponse({ error: "not_found" }, 404);
      }
      if (request.method !== "POST") {
        return jsonResponse(
          { error: "method_not_allowed" },
          405,
          { Allow: "POST" },
        );
      }

      try {
        const configuration = readConfiguration(env);
        const credentialRequest = await parseCredentialRequest(request);
        const claims = await verifyGithubToken(
          bearerToken(request),
          configuration.audience,
        );
        const policy = authorizeRole(claims, credentialRequest.role);
        const credentials = await createR2Credentials(
          configuration,
          policy,
          now,
        );
        return jsonResponse(credentials);
      } catch (error) {
        return errorResponse(error);
      }
    },
  };
}

export const protocol = Object.freeze({
  githubIssuer: GITHUB_ISSUER,
  githubJwksUrl: GITHUB_JWKS_URL,
  githubRepository: GITHUB_REPOSITORY,
  githubRepositoryId: GITHUB_REPOSITORY_ID,
  githubRepositoryOwnerId: GITHUB_REPOSITORY_OWNER_ID,
  githubSubjectPrefix: GITHUB_SUBJECT_PREFIX,
  githubAudience: GITHUB_AUDIENCE,
  requestSchema: REQUEST_SCHEMA,
  responseSchema: RESPONSE_SCHEMA,
  r2Prefix: R2_PREFIX,
  region: REGION,
  roles: ROLE_DEFINITIONS,
  workflows: Object.freeze({
    system: SYSTEM_WORKFLOW,
    packages: PACKAGES_WORKFLOW,
    ryzenBuild: RYZEN_BUILD_WORKFLOW,
    pin: PIN_WORKFLOW,
    release: RELEASE_WORKFLOW,
    broker: BROKER_WORKFLOW,
  }),
});

export default createBroker();
