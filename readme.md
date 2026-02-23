<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="art/dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="art/light.svg">
    <img alt="Ludos" src="art/dark.svg" width="750">
  </picture>
</p>

# Ludos
**Ludos** is an end-to-end OS image builder for rpm-based Linux distributions. You begin with an rpm-based Linux distribution such as Fedora and a set of cards. The cards contain information about how to build/collect a certain subset of packages with your changes (e.g., KDE, Gnome, Nvidia drivers, dev tools). Finally, you combine a set of cards to form a deck, which is the final image.

Unmodified packages can be collected from the distribution's repositories, or rebuilt using package sources to ensure provenance. Cards define a multi-stage build process that is cached at the card level, ensuring you only need to rebuild cards that have changed. An SLSA3 chain ensures provenance end-to-end, and Ludos is designed to run completely in CI/CD pipelines. At the same time, it has a weak dependency to Github Actions, allowing it to run locally or in other CI/CD environments.