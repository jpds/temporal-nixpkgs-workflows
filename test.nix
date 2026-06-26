{
  nixpkgsRadarWorker,
  pkgs,
  # Path to a GGUF model file used by llama-server.
  model,
  ...
}:

let
  # A minimal nixpkgs-like repo with three branches carrying different
  # package versions, used to exercise the gix and snix activities.
  mockNixpkgsRepo =
    pkgs.runCommand "mock-nixpkgs-repo"
      {
        nativeBuildInputs = [ pkgs.git ];
      }
      ''
              export HOME=$(mktemp -d)
              git config --global user.email "ci@test.dev"
              git config --global user.name "CI"

              WORK=$(mktemp -d)
              cd "$WORK"
              git init
              git checkout -b master

              mkdir -p pkgs/by-name/he/hello
              mkdir -p pkgs/by-name/wo/world

              cat > default.nix <<'EOF'
        { lib, system ? "x86_64-linux", ... }:
        {
          hello = {
            pname = "hello";
            version = "2.12.1";
            meta = {
              maintainers = with lib.maintainers; [ testuser ];
              homepage = "https://github.com/testuser/hello";
            };
          };
          world = {
            pname = "world";
            version = "1.0.0";
            meta = {
              maintainers = with lib.maintainers; [ testuser ];
              homepage = "https://github.com/testuser/world";
            };
          };
          other = {
            pname = "other";
            version = "3.0.0";
            meta = {
              maintainers = with lib.maintainers; [ somebodyelse ];
              homepage = "https://github.com/somebodyelse/other";
            };
          };
        }
        EOF

              cat > pkgs/by-name/he/hello/package.nix <<'EOF'
        { lib, ... }: {
          pname = "hello";
          version = "2.12.1";
          meta = {
            maintainers = with lib.maintainers; [ testuser ];
            homepage = "https://github.com/testuser/hello";
          };
        }
        EOF

              cat > pkgs/by-name/wo/world/package.nix <<'EOF'
        { lib, ... }: {
          pname = "world";
          version = "1.0.0";
          meta = {
            maintainers = with lib.maintainers; [ testuser ];
            homepage = "https://github.com/testuser/world";
          };
        }
        EOF

              git add -A
              git commit -m "master: hello 2.12.1, world 1.0.0"

              git checkout -b nixpkgs-unstable
              sed -i 's/version = "2\.12\.1"/version = "2.13.0"/g' default.nix
              sed -i 's/version = "2\.12\.1"/version = "2.13.0"/g' pkgs/by-name/he/hello/package.nix
              git add -A
              git commit -m "nixpkgs-unstable: hello 2.13.0"

              git checkout master
              git checkout -b nixos-26.05
              sed -i 's/version = "2\.12\.1"/version = "2.12.0"/g' default.nix
              sed -i 's/version = "2\.12\.1"/version = "2.12.0"/g' pkgs/by-name/he/hello/package.nix
              git add -A
              git commit -m "nixos-26.05: hello 2.12.0"

              git checkout master

              git clone --bare . "$out"
      '';

  githubMockRoot = pkgs.linkFarm "github-mock-root" [
    {
      name = "hello-latest.json";
      path = pkgs.writeText "hello-latest.json" (builtins.toJSON { tag_name = "v2.14.0"; });
    }
    {
      name = "world-latest.json";
      path = pkgs.writeText "world-latest.json" (builtins.toJSON { tag_name = "v1.1.0"; });
    }
  ];

  githubMockCaddyfile = pkgs.writeText "Caddyfile" ''
    :8080 {
      root * ${githubMockRoot}

      handle /repos/testuser/hello/releases/latest {
        rewrite * /hello-latest.json
        file_server
      }
      handle /repos/testuser/world/releases/latest {
        rewrite * /world-latest.json
        file_server
      }
      respond 404
    }
  '';
in
{
  name = "temporal-nixpkgs-radar";

  nodes = {

    nixpkgs-git =
      { pkgs, lib, ... }:
      {
        networking.firewall.allowedTCPPorts = [ 9418 ];

        systemd.services.git-daemon = {
          description = "Mock nixpkgs git daemon";
          after = [ "network.target" ];
          wantedBy = [ "multi-user.target" ];
          serviceConfig = {
            ExecStart = lib.escapeShellArgs [
              "${pkgs.git}/bin/git"
              "daemon"
              "--reuseaddr"
              "--base-path=/var/git"
              "--export-all"
              "--verbose"
              "--listen=0.0.0.0"
              "--listen=::"
              "--port=9418"
            ];
            Restart = "on-failure";
          };
        };

        systemd.tmpfiles.rules = [
          "d /var/git 0755 root root -"
        ];

        system.activationScripts.mockNixpkgs = ''
          mkdir -p /var/git
          cp -r ${mockNixpkgsRepo} /var/git/nixpkgs
          chmod -R u+w /var/git/nixpkgs
        '';

        virtualisation.memorySize = 512;
      };

    github-mock =
      { ... }:
      {
        networking.firewall.allowedTCPPorts = [ 8080 ];

        services.caddy = {
          enable = true;
          configFile = githubMockCaddyfile;
        };

        virtualisation.memorySize = 512;
      };

    llm =
      { ... }:
      {
        networking.firewall.allowedTCPPorts = [ 8080 ];

        services.llama-cpp = {
          enable = true;
          settings = {
            model = model;
            host = "0.0.0.0";
            port = 8080;
          };
        };

        virtualisation = {
          memorySize = 4 * 1024;
          cores = 2;
        };
      };

    temporal =
      { pkgs, ... }:
      {
        networking.firewall.allowedTCPPorts = [ 7233 ];

        environment.systemPackages = [
          pkgs.grpc-health-probe
          pkgs.temporal-cli
        ];

        services.temporal = {
          enable = true;
          settings = {
            log = {
              stdout = true;
              level = "info";
            };
            services = {
              frontend.rpc = {
                grpcPort = 7233;
                membershipPort = 6933;
                bindOnIP = "0.0.0.0";
                httpPort = 7243;
              };
              matching.rpc = {
                grpcPort = 7235;
                membershipPort = 6935;
                bindOnLocalHost = true;
              };
              history.rpc = {
                grpcPort = 7234;
                membershipPort = 6934;
                bindOnLocalHost = true;
              };
              worker.rpc = {
                grpcPort = 7239;
                membershipPort = 6939;
                bindOnLocalHost = true;
              };
            };
            global.membership = {
              maxJoinDuration = "30s";
              broadcastAddress = "0.0.0.0";
            };
            persistence = {
              defaultStore = "sqlite-default";
              visibilityStore = "sqlite-visibility";
              numHistoryShards = 1;
              datastores = {
                sqlite-default.sql = {
                  pluginName = "sqlite";
                  databaseName = "default";
                  connectAddr = "localhost";
                  connectProtocol = "tcp";
                  connectAttributes = {
                    mode = "memory";
                    cache = "private";
                  };
                  maxConns = 1;
                  maxIdleConns = 1;
                  maxConnLifetime = "1h";
                };
                sqlite-visibility.sql = {
                  pluginName = "sqlite";
                  databaseName = "default";
                  connectAddr = "localhost";
                  connectProtocol = "tcp";
                  connectAttributes = {
                    mode = "memory";
                    cache = "private";
                  };
                  maxConns = 1;
                  maxIdleConns = 1;
                  maxConnLifetime = "1h";
                };
              };
            };
            clusterMetadata = {
              enableGlobalNamespace = false;
              failoverVersionIncrement = 10;
              masterClusterName = "active";
              currentClusterName = "active";
              clusterInformation.active = {
                enabled = true;
                initialFailoverVersion = 1;
                rpcName = "frontend";
                rpcAddress = "temporal:7233";
                httpAddress = "temporal:7243";
              };
            };
            dcRedirectionPolicy.policy = "noop";
          };
        };

        virtualisation.cores = 2;
      };

    worker =
      { pkgs, lib, ... }:
      {
        environment.systemPackages = [ pkgs.temporal-cli pkgs.git ];

        systemd.services.nixpkgs-radar = {
          after = [ "network-online.target" ];
          wants = [ "network-online.target" ];
          unitConfig.ConditionPathExists = "/etc/nixpkgs-radar/env";
          serviceConfig = {
            ExecStart = "${lib.getExe' nixpkgsRadarWorker "nixpkgs-radar-worker"}";
            EnvironmentFile = "/etc/nixpkgs-radar/env";
            Restart = "on-failure";
            RestartSec = "2s";
            DynamicUser = true;
            StateDirectory = "nixpkgs-radar";
          };
        };

        virtualisation = {
          memorySize = 4 * 1024;
          cores = 2;
          diskSize = 8 * 1024;
        };
      };

  };

  testScript = ''
    import json

    nixpkgs_git.start()
    nixpkgs_git.wait_for_unit("git-daemon.service")
    nixpkgs_git.wait_for_open_port(9418)

    github_mock.start()
    github_mock.wait_for_unit("caddy.service")
    github_mock.wait_for_open_port(8080)

    llm.start()
    llm.wait_for_unit("llama-cpp.service")
    llm.wait_for_open_port(8080)
    llm.wait_until_succeeds("curl --fail -s http://localhost:8080/health")

    temporal.start()
    temporal.wait_for_unit("temporal.service")
    temporal.wait_for_open_port(7233)
    temporal.wait_until_succeeds(
        "grpc-health-probe -addr=127.0.0.1:7233"
        " -service=temporal.api.workflowservice.v1.WorkflowService"
    )
    temporal.wait_until_succeeds(
        "journalctl -o cat -u temporal.service | grep 'Frontend is now healthy'"
    )
    temporal.log(
        temporal.wait_until_succeeds(
            "temporal operator namespace create --namespace radar --address 127.0.0.1:7233",
            timeout=60,
        )
    )

    worker.start()
    worker.systemctl("start network-online.target")
    worker.wait_for_unit("network-online.target")

    worker.succeed("""
        mkdir -p /etc/nixpkgs-radar
        cat > /etc/nixpkgs-radar/env <<'ENVEOF'
    NIXPKGS_URL=git://nixpkgs-git/nixpkgs
    NIXPKGS_CACHE_PATH=/var/lib/nixpkgs-radar/nixpkgs
    GITHUB_API_BASE_URL=http://github-mock:8080
    OPENAI_BASE_URL=http://llm:8080/v1
    OPENAI_API_KEY=not-needed
    MODEL=local
    TEMPORAL_ADDRESS=temporal:7233
    TEMPORAL_NAMESPACE=radar
    TEMPORAL_TASK_QUEUE=nixpkgs-radar-queue
    ENVEOF
    """)

    worker.systemctl("start nixpkgs-radar.service")
    worker.wait_for_unit("nixpkgs-radar.service")

    worker.wait_until_succeeds(
        "git ls-remote git://nixpkgs-git/nixpkgs HEAD",
        timeout=60,
    )

    with temporal.nested("Run NixpkgsRadarWorkflow for testuser"):
        temporal.succeed(
            "temporal workflow start"
            " --namespace radar"
            " --address 127.0.0.1:7233"
            " --type NixpkgsRadarWorkflow"
            " --task-queue nixpkgs-radar-queue"
            " --workflow-id radar-test-1"
            """ --input '"testuser"'"""
        )

        result_json = json.loads(
            temporal.wait_until_succeeds(
                "temporal workflow result"
                " --namespace radar"
                " --address 127.0.0.1:7233"
                " --workflow-id radar-test-1"
                " --output json",
                timeout=600,
            )
        )

        assert result_json["status"] == "COMPLETED", (
            f"NixpkgsRadarWorkflow did not complete: {result_json}"
        )
        output = result_json.get("result", "")
        assert output, "NixpkgsRadarWorkflow returned empty result"
        assert "hello" in output.lower(), (
            f"Expected 'hello' in output, got: {output}"
        )
        temporal.log(f"Radar report:\n{output}")
  '';
}
