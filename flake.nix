{
  description = "Temporal workflow that monitors nixpkgs packages for a GitHub maintainer";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    snix-src = {
      url = "git+https://git.snix.dev/snix/snix?rev=6726192635c063576a50374134a0fe334b17b334";
      flake = false;
    };
  };

  outputs =
    {
      nixpkgs,
      flake-utils,
      snix-src,
      ...
    }:
    flake-utils.lib.eachSystem [ "x86_64-linux" "aarch64-linux" ] (
      system:
      let
        pkgs = import nixpkgs { inherit system; };
        inherit (pkgs) lib;
        rustPlatform = pkgs.rustPlatform;

        nixpkgsGixSrc = lib.fileset.toSource {
          root = ./nixpkgs_gix;
          fileset = lib.fileset.gitTracked ./nixpkgs_gix;
        };

        nixpkgsSnixSrc = lib.fileset.toSource {
          root = ./nixpkgs_snix;
          fileset = lib.fileset.gitTracked ./nixpkgs_snix;
        };

        nixpkgsRadarSrc = lib.fileset.toSource {
          root = ./nixpkgs_radar;
          fileset = lib.fileset.gitTracked ./nixpkgs_radar;
        };

        pyTag = "cp" + builtins.replaceStrings [ "." ] [ "" ] pkgs.python3.pythonVersion;
        wheelPlatform = "linux_" + pkgs.stdenv.hostPlatform.parsed.cpu.name;
        wheelSuffix = "${pyTag}-${pyTag}-${wheelPlatform}";

        python3 = pkgs.python3.override {
          packageOverrides = self: super: {

            griffe = self.griffelib;

            openai = super.openai.overridePythonAttrs (_: rec {
              version = "2.41.1";
              src = pkgs.fetchPypi {
                pname = "openai";
                inherit version;
                hash = "sha256-I9YXoEMkV62ESXO+6PVAvp2pCJT3xWhoUtLTZdoFj1c=";
              };
              pythonRelaxDeps = true;
            });

            openai-agents = super.openai-agents.overridePythonAttrs (_: rec {
              version = "0.17.5";
              src = pkgs.fetchPypi {
                pname = "openai_agents";
                inherit version;
                hash = "sha256-XdRpQ7mT4aaKeKzSVPxqAM8EVfw9zIAgeOomlksUJ4w=";
              };
              pythonRelaxDeps = true;
              propagatedBuildInputs = with self; [
                griffelib
                mcp
                openai
                pydantic
                types-requests
                typing-extensions
                websockets
              ];
            });

          };
        };

        _snixGitDeps = {
          "nix-compat-0.1.0" = "sha256-rapgqasf4yMCNpDKKkRpdNJstsWcurNzOAabroz1I6U=";
          "nix-compat-derive-0.1.0" = "sha256-rapgqasf4yMCNpDKKkRpdNJstsWcurNzOAabroz1I6U=";
          "snix-build-0.1.0" = "sha256-rapgqasf4yMCNpDKKkRpdNJstsWcurNzOAabroz1I6U=";
          "snix-build-glue-0.1.0" = "sha256-rapgqasf4yMCNpDKKkRpdNJstsWcurNzOAabroz1I6U=";
          "snix-castore-0.1.0" = "sha256-rapgqasf4yMCNpDKKkRpdNJstsWcurNzOAabroz1I6U=";
          "snix-eval-0.1.0" = "sha256-rapgqasf4yMCNpDKKkRpdNJstsWcurNzOAabroz1I6U=";
          "snix-eval-builtin-macros-0.0.1" = "sha256-rapgqasf4yMCNpDKKkRpdNJstsWcurNzOAabroz1I6U=";
          "snix-glue-0.1.0" = "sha256-rapgqasf4yMCNpDKKkRpdNJstsWcurNzOAabroz1I6U=";
          "snix-store-0.1.0" = "sha256-rapgqasf4yMCNpDKKkRpdNJstsWcurNzOAabroz1I6U=";
          "snix-tracing-0.1.0" = "sha256-rapgqasf4yMCNpDKKkRpdNJstsWcurNzOAabroz1I6U=";
          "hyper-1.10.1" = "sha256-5Jwxx+cafnawCBV+6VS461uL2TGht8k6xPBf2tAhcO0=";
          "tonic-0.14.6" = "sha256-tl2Zqbt26+PfNE5TO/7ITH3VXhf3KUpr26rgennfhj4=";
          "tonic-build-0.14.6" = "sha256-tl2Zqbt26+PfNE5TO/7ITH3VXhf3KUpr26rgennfhj4=";
          "tonic-prost-0.14.6" = "sha256-tl2Zqbt26+PfNE5TO/7ITH3VXhf3KUpr26rgennfhj4=";
          "tonic-prost-build-0.14.6" = "sha256-tl2Zqbt26+PfNE5TO/7ITH3VXhf3KUpr26rgennfhj4=";
          "wu-manber-0.1.0" = "sha256-7YIttaQLfFC/32utojh2DyOHVsZiw8ul/z0lvOhAE/4=";
        };

        nixpkgsGixWheel = pkgs.stdenv.mkDerivation {
          pname = "nixpkgs_gix-wheel";
          version = "0.1.0";
          src = nixpkgsGixSrc;

          cargoDeps = rustPlatform.importCargoLock {
            lockFile = ./nixpkgs_gix/Cargo.lock;
          };

          nativeBuildInputs = [
            pkgs.cargo
            pkgs.rustc
            pkgs.maturin
            pkgs.python3
            rustPlatform.cargoSetupHook
          ];

          buildPhase = ''
            maturin build --release --offline
          '';

          installPhase = ''
            mkdir -p $out
            cp target/wheels/*.whl $out/
          '';
        };

        nixpkgsGix = python3.pkgs.buildPythonPackage {
          pname = "nixpkgs_gix";
          version = "0.1.0";
          format = "wheel";
          src = "${nixpkgsGixWheel}/nixpkgs_gix-0.1.0-${wheelSuffix}.whl";
          doCheck = false;
        };

        nixpkgsSnixWheel = pkgs.stdenv.mkDerivation {
          pname = "nixpkgs_snix-wheel";
          version = "0.1.0";
          src = nixpkgsSnixSrc;

          cargoDeps = rustPlatform.importCargoLock {
            lockFile = ./nixpkgs_snix/Cargo.lock;
            outputHashes = _snixGitDeps;
          };

          nativeBuildInputs = [
            pkgs.cargo
            pkgs.rustc
            pkgs.maturin
            pkgs.protobuf
            pkgs.python3
            rustPlatform.cargoSetupHook
          ];

          PROTOC = "${pkgs.protobuf}/bin/protoc";
          PROTO_ROOT = snix-src;
          SNIX_BUILD_SANDBOX_SHELL = "/bin/sh";

          buildPhase = ''
            maturin build --release --offline
          '';

          installPhase = ''
            mkdir -p $out
            cp target/wheels/*.whl $out/
          '';
        };

        nixpkgsSnix = python3.pkgs.buildPythonPackage {
          pname = "nixpkgs_snix";
          version = "0.1.0";
          format = "wheel";
          src = "${nixpkgsSnixWheel}/nixpkgs_snix-0.1.0-${wheelSuffix}.whl";
          doCheck = false;
        };

        nixpkgsRadarLib = python3.pkgs.buildPythonPackage {
          pname = "nixpkgs-radar-lib";
          version = "1.0.0";
          pyproject = false;

          src = nixpkgsRadarSrc;
          dontUnpack = true;

          installPhase = ''
            mkdir -p $out/${python3.sitePackages}
            cp -r $src $out/${python3.sitePackages}/nixpkgs_radar
          '';

          propagatedBuildInputs = with python3.pkgs; [
            nixpkgsGix
            nixpkgsSnix
            openai-agents
            opentelemetry-api
            opentelemetry-sdk
            temporalio
          ];

          pythonImportsCheck = [ "nixpkgs_radar" ];

          meta = with pkgs.lib; {
            description = "Temporal workflow for monitoring nixpkgs packages";
            license = licenses.mit;
          };
        };

        nixpkgsRadarWorker = python3.pkgs.buildPythonApplication {
          pname = "nixpkgs-radar-worker";
          version = "1.0.0";
          pyproject = false;

          dontUnpack = true;

          installPhase = ''
            install -Dm755 ${./nixpkgs-radar-worker.py} $out/bin/nixpkgs-radar-worker
          '';

          propagatedBuildInputs = [
            nixpkgsRadarLib
            python3.pkgs.temporalio
          ];

          meta = with pkgs.lib; {
            description = "Temporal nixpkgs radar worker";
            license = licenses.mit;
          };
        };

      in
      {
        packages.nixpkgsGix = nixpkgsGix;
        packages.nixpkgsSnix = nixpkgsSnix;
        packages.nixpkgsRadarLib = nixpkgsRadarLib;
        packages.nixpkgsRadarWorker = nixpkgsRadarWorker;
        packages.default = nixpkgsRadarWorker;

        devShells.default = pkgs.mkShell {
          packages = [
            pkgs.ruff
            pkgs.temporal-cli
            pkgs.cargo
            pkgs.rustc
            pkgs.maturin
            pkgs.protobuf
            (python3.withPackages (ps: [
              nixpkgsGix
              nixpkgsSnix
              ps.openai-agents
              ps.opentelemetry-api
              ps.opentelemetry-sdk
              ps.temporalio
            ]))
          ];

          env = {
            SNIX_BUILD_SANDBOX_SHELL = "/bin/sh";
            PROTOC = "${pkgs.protobuf}/bin/protoc";
          };
        };

        checks.nixpkgs-radar = pkgs.testers.runNixOSTest (
          import ./test.nix {
            inherit nixpkgsRadarWorker pkgs;
            model = pkgs.fetchurl {
              url = "https://huggingface.co/unsloth/gemma-4-E2B-it-qat-GGUF/resolve/45dde4a86b6c5dce72297198762d2e8e68c0cbd4/gemma-4-E2B-it-qat-UD-Q4_K_XL.gguf";
              hash = "sha256-zUUmST3Mv9Z5G+6IIuN+MDQAdNHU2araUs4Jr+/Wozo=";
            };
          }
        );
      }
    );
}
