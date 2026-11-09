{
  description = "Power-cycle crash-recovery test orchestrator for databases";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
  };

  outputs = { self, nixpkgs }:
    let
      supportedSystems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
    in
    {
      packages = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          pyPkgs = pkgs.python3Packages;
          runtimeBins = [
            pkgs.ipmitool
            pkgs.openssh
          ];
        in
        rec {
          power-cycle = pyPkgs.buildPythonApplication {
            pname = "power-cycle";
            version = "0.1.0";
            pyproject = true;

            src = ./.;

            build-system = [
              pyPkgs.setuptools
            ];

            dependencies = [
              pyPkgs.fabric
              pyPkgs.invoke
              pyPkgs.paramiko
            ];

            makeWrapperArgs = [
              "--prefix"
              "PATH"
              ":"
              (pkgs.lib.makeBinPath runtimeBins)
            ];

            doCheck = false;
            pythonImportsCheck = [
              "power_cycle"
              "power_cycle.config"
              "power_cycle.runner"
              "power_cycle.orchestrator"
            ];

            meta = {
              description = "Power-cycle crash-recovery test orchestrator for databases";
              mainProgram = "power-cycle";
            };
          };

          default = power-cycle;
        }
      );

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/power-cycle";
        };
      });

      devShells = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          pythonEnv = pkgs.python3.withPackages (ps: [
            ps.fabric
            ps.invoke
            ps.paramiko
          ]);
        in
        {
          default = pkgs.mkShell {
            packages = [
              pythonEnv
              pkgs.ipmitool
              pkgs.openssh
            ];
          };
        }
      );
    };
}
