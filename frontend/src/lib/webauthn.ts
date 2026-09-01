function decodeBase64url(value: string): ArrayBuffer {
  const base64 = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = base64.padEnd(Math.ceil(base64.length / 4) * 4, "=");
  const binary = window.atob(padded);
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  return bytes.buffer;
}

function encodeBase64url(value: ArrayBuffer | null): string | null {
  if (value === null) return null;
  const bytes = new Uint8Array(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return window.btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export async function createPasskey(
  options: PublicKeyCredentialCreationOptionsJSON,
): Promise<Record<string, unknown>> {
  if (!window.PublicKeyCredential || !navigator.credentials) {
    throw new Error("This browser does not support passkeys.");
  }
  const publicKey: PublicKeyCredentialCreationOptions = {
    rp: options.rp,
    challenge: decodeBase64url(options.challenge),
    user: { ...options.user, id: decodeBase64url(options.user.id) },
    pubKeyCredParams: options.pubKeyCredParams.map((item) => ({
      alg: item.alg,
      type: "public-key" as const,
    })),
    timeout: options.timeout,
    excludeCredentials: options.excludeCredentials?.map((item) => ({
      type: "public-key" as const,
      id: decodeBase64url(item.id),
      transports: item.transports as AuthenticatorTransport[] | undefined,
    })),
    authenticatorSelection:
      options.authenticatorSelection as AuthenticatorSelectionCriteria | undefined,
    attestation: options.attestation as AttestationConveyancePreference | undefined,
  };
  const created = (await navigator.credentials.create({ publicKey })) as PublicKeyCredential | null;
  if (!created) throw new Error("Passkey creation was canceled.");
  const response = created.response as AuthenticatorAttestationResponse;
  return {
    id: created.id,
    rawId: encodeBase64url(created.rawId),
    type: created.type,
    authenticatorAttachment: created.authenticatorAttachment,
    clientExtensionResults: created.getClientExtensionResults(),
    response: {
      clientDataJSON: encodeBase64url(response.clientDataJSON),
      attestationObject: encodeBase64url(response.attestationObject),
      transports: response.getTransports?.() ?? [],
    },
  };
}

export async function authenticatePasskey(
  options: PublicKeyCredentialRequestOptionsJSON,
): Promise<Record<string, unknown>> {
  if (!window.PublicKeyCredential || !navigator.credentials) {
    throw new Error("This browser does not support passkeys.");
  }
  const publicKey: PublicKeyCredentialRequestOptions = {
    challenge: decodeBase64url(options.challenge),
    rpId: options.rpId,
    timeout: options.timeout,
    userVerification: options.userVerification as UserVerificationRequirement | undefined,
    allowCredentials: options.allowCredentials?.map((item) => ({
      type: "public-key" as const,
      id: decodeBase64url(item.id),
      transports: item.transports as AuthenticatorTransport[] | undefined,
    })),
  };
  const received = (await navigator.credentials.get({ publicKey })) as PublicKeyCredential | null;
  if (!received) throw new Error("Passkey authorization was canceled.");
  const response = received.response as AuthenticatorAssertionResponse;
  return {
    id: received.id,
    rawId: encodeBase64url(received.rawId),
    type: received.type,
    authenticatorAttachment: received.authenticatorAttachment,
    clientExtensionResults: received.getClientExtensionResults(),
    response: {
      authenticatorData: encodeBase64url(response.authenticatorData),
      clientDataJSON: encodeBase64url(response.clientDataJSON),
      signature: encodeBase64url(response.signature),
      userHandle: encodeBase64url(response.userHandle),
    },
  };
}
