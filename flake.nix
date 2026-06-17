{
  description = "Odysseus development shell: Python (uv-managed ./venv) + Node toolchain for running the app and its test suite.";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { nixpkgs, ... }:
    let
      inherit (nixpkgs) lib;
      forAllSystems = lib.genAttrs lib.systems.flakeExposed;
    in
    {
      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};

          # The app supports Python 3.11+ (see docs/setup.md). Mirror the
          # production image (Dockerfile: python:3.14-slim) so the dev shell
          # catches version-specific issues before they ship. uv builds ./venv
          # against this interpreter; change it to bump/downgrade in lockstep.
          python = pkgs.python314;
        in
        {
          default = pkgs.mkShell {
            packages = [
              python
              pkgs.uv # fast venv + wheel installer (docs/setup.md: "Faster installs with uv")
              pkgs.nodejs # JS/bombadil tests + the Browser MCP server (npx)
              pkgs.git
              pkgs.cmake # native/source builds — parity with the Dockerfile
              pkgs.pkg-config
              pkgs.tmux # Cookbook background downloads/serves
              pkgs.openssh # Cookbook remote-server tests, setup, probes
            ];

            # Prebuilt (manylinux) wheels — numpy, onnxruntime via fastembed,
            # pydantic-core, cryptography, lxml, … — dlopen native shared
            # objects that are not on NixOS's default loader path. Expose the
            # manylinux baseline plus libstdc++/zlib so those wheels load.
            env = lib.optionalAttrs pkgs.stdenv.isLinux {
              LD_LIBRARY_PATH = lib.makeLibraryPath (
                (with pkgs; [
                  stdenv.cc.cc.lib
                  zlib
                ])
                ++ pkgs.pythonManylinuxPackages.manylinux1
              );
            };

            shellHook = ''
              unset PYTHONPATH
              export UV_PYTHON_DOWNLOADS=never   # use the Nix interpreter, never one uv downloads

              # Create the project-local ./venv (docs + tests/run_focus.py expect
              # this path) against the Nix interpreter. If an existing ./venv was
              # built for a different Python (e.g. after changing the pin above),
              # rebuild it instead of silently reusing the stale interpreter.
              if [ -e venv/bin/python ]; then
                want="${python.pythonVersion}"
                have="$(venv/bin/python -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
                if [ "$have" != "$want" ]; then
                  echo "odysseus: ./venv is Python $have but the flake pins $want — recreating ./venv"
                  rm -rf venv
                fi
              fi
              [ -e venv/bin/python ] || uv venv venv --python "${python.interpreter}"
              source venv/bin/activate

              # Install deps until they succeed once (stamp guards against a
              # half-finished install being treated as complete). Prefer the
              # optional, reproducible requirements.lock when present.
              if [ ! -e venv/.odysseus-deps-installed ]; then
                echo "odysseus: installing dependencies into ./venv via uv…"
                if [ -f requirements.lock ]; then
                  uv pip sync requirements.lock && touch venv/.odysseus-deps-installed
                else
                  uv pip install -r requirements.txt && touch venv/.odysseus-deps-installed
                fi
              fi

              echo ""
              echo "odysseus dev shell — ./venv active (Python $(python -V 2>&1 | cut -d' ' -f2))"
              echo "  tests:          python -m pytest            # or: python tests/run_focus.py --fast"
              echo "  update deps:    uv pip install -r requirements.txt"
              echo "  optional deps:  uv pip install -r requirements-optional.txt"
              echo "  run server:     python -m uvicorn app:app --host 127.0.0.1 --port 7000"
            '';
          };
        }
      );
    };
}
