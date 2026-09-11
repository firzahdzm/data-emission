"""Entry point: `python -m emission_tracker.signer /etc/emission-signer/config.yaml`"""

import logging
import os
import sys

import yaml

from emission_tracker.signer.server import Signer, SignerConfig, serve


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config_path = sys.argv[1] if len(sys.argv) > 1 else "/etc/emission-signer/config.yaml"
    raw = yaml.safe_load(open(config_path))
    socket_path = raw.pop("socket_path", "/run/emission-signer.sock")
    raw.setdefault(
        "credentials_dir", os.environ.get("CREDENTIALS_DIRECTORY", "/run/credentials")
    )
    serve(socket_path, Signer(SignerConfig(**raw)))


if __name__ == "__main__":
    main()
