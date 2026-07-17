<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="art/dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="art/light.svg">
    <img alt="Ludos" src="art/dark.svg" width="750">
  </picture>
</p>

# Ludos
**Ludos** is an end-to-end OS image builder for rpm-based Linux distributions. You begin with an rpm-based Linux distribution such as Fedora and a set of cards. The cards contain information about how to build/collect a certain subset of packages with your changes (e.g., KDE, Gnome, Nvidia drivers, dev tools). Finally, you combine a set of cards to form a deck, which is the final image.

Unmodified packages can be collected from the distribution's repositories, or rebuilt using package sources to ensure provenance. Cards define a multi-stage build process that is cached at the card level, ensuring you only need to rebuild cards that have changed. An SLSA L3 chain (TODO) ensures provenance end-to-end, and Ludos is designed to run completely in CI/CD pipelines. At the same time, it has a weak dependency to Github Actions, allowing it to run locally or in other CI/CD environments.

## Roadmap

The initial versions of Ludos focus on ensuring end-user functionality. I.e., stable and fast builds. Once that is achieved to a high degree and Ludos stabilizes, work will focus on provenance. Specifically, complying with SLSA Build L3 and Source L3, with Ludos producing detailed SPDX SBOMS. For SLSA Source L4, a meta-release process will be created to certify a release by two people during promotions to a stable channel. That means that the builds produced by Ludos will never be Source L4. This is practical: pinning every single dependency in a repository encourages trash commits and is fragile. It is much better to let dependencies such as upstream packages / files float, then test the build / certify its inputs post-creation.

## Contributing

Ludos does not currently accept external contributions. You are welcome to post issues in the issue tracker, with suggestions or bug reports.

## License

Copyright (C) 2026 Antheas Kapenekakis

A copy of Ludos is provided to you under the terms of [GNU Affero General Public License v3.0 or later](LICENSE).
