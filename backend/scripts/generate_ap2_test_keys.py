"""Print four independent P-256 test keys as dotenv-safe values.

Run this locally and paste the output into backend/.env. The command never writes
key material to disk, and these test issuers must not be reused in production.
"""

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

VARIABLES = (
    "AP2_MERCHANT_PRIVATE_KEY_PEM",
    "AP2_AGENT_PROVIDER_PRIVATE_KEY_PEM",
    "AP2_CREDENTIALS_PROVIDER_PRIVATE_KEY_PEM",
    "AP2_PAYMENT_PROCESSOR_PRIVATE_KEY_PEM",
)


def dotenv_private_key() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode().strip().replace("\n", "\\n")


if __name__ == "__main__":
    print("# Development-only AP2 ES256 keys. Keep this output secret.")
    for variable in VARIABLES:
        print(f"{variable}={dotenv_private_key()}")
