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

## Features
Ludos is a complete immutable distribution creation toolkit. Unlike existing tools, it covers the whole creation and distribution process, producing fully featured images, installer ISOs, preinstalled applications and a hosted backend for all of them in S3.

### Building

#### Custom Packages
Ludos can fork and track dist-git packages with override patches on top of the original source code, while automatically updating them based on upstream and performing conflict resolution.

#### Custom Applications
Ludos supports creating custom Flatpak applications that leverage the operating system image as the runtime and derive from Fedora Flatpaks. By re-using the OS as a runtime, updates and your ISO are smaller. And thanks to the work behind Fedora Flatpaks, around 800 applications are available for you to customize and preload for your users with little work.

Ludos watermarks the icon and supports renaming the author field of Flatpaks. This way, you can differentiate your flatpaks from other remotes and direct users to your issue tracker.

#### Freeze the World (Manual Fedora Syncs)

Ludos freezes Fedora repositories and caches all packages (both for the image and your package builders) based on a weekly version key (last Monday's date based on UTC-0). This way, the normal churn of tweaking your image does not bring surprises and packages update once a week.

#### Hot Reloads and Performance

Ludos uses a multi-layer caching system optimized for both CI builders and your local machine, including a fast deploy path through ostree. On startup, Ludos uses its repository cache and hashes the build inputs of all your cards to see which cards, builders, or rpm caches need rebuilding. Then, it only builds those. Afterwards, it creates a multi-layer containerfile that sequentially installs the packages of each card and then applies the postprocess steps of each card.

**Locally:** I.e., you change the packages of card 6? You start from the step that installs its packages. You change the postprocess steps of card 6 or they crash? You begin with all packages having been installed and the postprocess steps of cards 1-5 already applied.

Then, ostree does a read-only pass to find changes in your new container and beams it to your device.

Time: **1 - 3 minutes** to do a local deploy with minor changes, on a **VM** or through **SSH**. The Anatase deploy scripts also use soft-reboot. **8 minutes** for a full build of Anatase (cached deps).

**On CI:** Automatic fanout of builds for your cards, and the multi-stage containerfile collapses to two stages, a single RPM install, and the application of postprocess steps (faster). Your local Ludos will also automatically pull from CI when build/card images are available, so your contributors don't have to rebuild everything locally.

### Distribution

#### Diffed updates

Ludos traces its roots in the [rechunk](https://github.com/hhd-dev/rechunk/) project, which provided partitioning rules for ostree-ext-rs (now `bootc internals ostree-ext`). The code for this analysis remains and got a fresh coat of paint. The major improvement Ludos introduces is processing speed.

Instead of hacky scripts that modify the container root in place using rootful podman (see [here](https://github.com/hhd-dev/rechunk/blob/master/1_prune.sh)), a tar reader scans your image and does path rewriting that is fed to ostree. This eliminates slow whiteout processing from doing modifications directly in that mount. In addition, ostree-ext was taught to parallelize, becoming 4x faster. Finally, to do this processing, ludos creates a container from your image, then mounts your image in that container, so it uses the **internal patched bootc** of that image. There is no [rechunk registry package](https://github.com/hhd-dev/rechunk/pkgs/container/rechunk), external dependency or pulls.

The tar rewriter and ostree backend are also used during local deploys, so test deploys and your public images are 1-1.

TLDR: **3x faster, more secure, cleaner (16m on 4 core builders -> 6m on 2 core builders; 2min locally)**

The device update story is also better compared to something like chunkah (which is not 1-1). ostree-ext pre-calculates the selinux policy and ostree metadata, so bootc happily re-uses them, resulting in **15 second updates with minor changes** and **no fan spin-up** (with the merge commit skip fix).

#### S3 backend

Ludos contains an optional S3 OCI registry implementation. To put it simply, you point Ludos to an S3 bucket, and it stores everything your distribution needs there: flatpaks, images, ISOs, and flatpak metadata. 

The reason is simple: S3 registries are typically **faster**, **cheaper**, and **more reliable** than container registries. This is because they do not need a database or to be consistent. If you ask for a layer you get that layer directly.

This does introduce some limitations that may make them unsuitable for your usecase, such as parallel writers interacting with the registry being able to corrupt it (e.g., a tree-shaker process can remove layers of an image that is currently uploaded causing it to corrupt). But if you have a dedicated bucket for your distribution CI, your CI's concurrency control can solve that.

**But GHCR is free?** That's true, for now, but you tie your updater infrastructure with a URI you don't control and Github lists a one month notice to introduce charging changes for it. If you do URL rewriting through Cloudflare Workers to still use it, you end up paying more than Cloudflare R2. So, just use cloudflare R2 and pay for the storage.

If you are a normal user and e.g., you want to customize Anatase for your uses, just skip the whole GHCR/Cosign/Github Actions dance, clone the Anatase repo, and build locally.

#### Strong Signing Policy

Even in this early stage, Ludos supports using FIPS HSM keys from Google Cloud to do both GPG signing for Flatpaks+ISOs, and Cosign signing for your images. Combine that with OIDC and IAM logging, and you have a credible security story.

Neither GPG or Cosign are used, with Ludos having a small re-implementation of the formats in it. Specifically for Cosign, Ludos uses both the legacy .sig and the new referrers format.

The reason for the re-implementation is that GPG does not support cloud keys. Well it does, if you combine libkmsp11 and a patched version of gnupg-pkcs11-sc. This dance is enough to create a sub-key but attempting to make that CI friendly and dependable with sub-30 second installs is not viable. As for cosign, it is a foreign dependency and wants to interact with a real registry or it does not work (we use an S3 read-only registry). Ludos will do signature verification with both gpg and cosign if you have them installed after it performs signing.

## Roadmap

The initial versions of Ludos focus on ensuring end-user functionality. I.e., stable and fast builds. Once that is achieved to a high degree and Ludos stabilizes, work will focus on provenance. Specifically, complying with SLSA Build L3 and Source L3, with Ludos producing detailed SPDX SBOMS. For SLSA Source L4, a meta-release process will be created to certify a release by two people during promotions to a stable channel. That means that the builds produced by Ludos will never be Source L4. This is practical, as pinning every single dependency in a repository encourages trash commits and is fragile. It is much better to let dependencies such as upstream packages / files float, then test the build / certify its inputs post-creation.

## Contributing

Ludos does not currently accept external contributions. You are welcome to post issues in the issue tracker, with suggestions or bug reports.

## License

A copy of Ludos is provided to you under the terms of [GNU Affero General Public License v3.0 or later](LICENSE).
