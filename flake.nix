{
  description = "Gobo — two-agent attention management over Telegram";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python313;
      in {
        devShells.default = pkgs.mkShell {
          packages = [
            python
            pkgs.uv
            pkgs.pulumi-bin
            pkgs.sqlite
            pkgs.ruff
          ];
          env = {
            # uv's downloaded standalone CPython builds don't run on NixOS;
            # pin uv to the nix-provided interpreter instead.
            UV_PYTHON_DOWNLOADS = "never";
            UV_PYTHON = "${python}/bin/python3";
          };
        };
      });
}
