"""Generate a Werkzeug password hash for ADMIN_PASSWORD_HASH.

Usage:
    python scripts/gen_password_hash.py 'my-secret-password'
"""
import sys

from werkzeug.security import generate_password_hash

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/gen_password_hash.py '<password>'")
        sys.exit(1)
    print(generate_password_hash(sys.argv[1]))
