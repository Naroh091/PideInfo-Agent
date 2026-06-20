# Changelog

## [0.10.0](https://github.com/Naroh091/PideInfo-Agent/compare/v0.9.0...v0.10.0) (2026-06-20)


### Features

* implement complaints for regional/local requests via CTBG, regional bodies via REG ([d89c211](https://github.com/Naroh091/PideInfo-Agent/commit/d89c211af94a04c5ae535583b20a3f7c269e4cca))

## [0.9.0](https://github.com/Naroh091/PideInfo-Agent/compare/v0.8.0...v0.9.0) (2026-06-09)


### Features

* re-link CTBG expedientes by document hash ([68cd583](https://github.com/Naroh091/PideInfo-Agent/commit/68cd583e80c1bc2f690aca62152924bea7ec81a2))
* re-link CTBG expedientes by document hash ([68cd583](https://github.com/Naroh091/PideInfo-Agent/commit/68cd583e80c1bc2f690aca62152924bea7ec81a2))
* re-link CTBG expedientes by document hash ([e23d75a](https://github.com/Naroh091/PideInfo-Agent/commit/e23d75ab351d747e0589e2a8462a49e45062c1d4))


### Bug Fixes

* ctbg hash sync ([df5130a](https://github.com/Naroh091/PideInfo-Agent/commit/df5130ae2e7664da4050e7516ee90ea2630b8b01))
* inmediate download of CTBG PDF files ([35eede7](https://github.com/Naroh091/PideInfo-Agent/commit/35eede7c415b941eaa4d5d669aaee6370577882c))

## [0.8.0](https://github.com/Naroh091/PideInfo-Agent/compare/v0.7.6...v0.8.0) (2026-05-20)


### Features

* gate uncertain tasks ([71c0fa2](https://github.com/Naroh091/PideInfo-Agent/commit/71c0fa29d5e9558b30ad9b2c8e51a20aa93e1659))

## [0.7.6](https://github.com/Naroh091/PideInfo-Agent/compare/v0.7.5...v0.7.6) (2026-05-20)


### Bug Fixes

* increase timeouts for Portal de Transparencia ([ff74494](https://github.com/Naroh091/PideInfo-Agent/commit/ff7449478cd8b2ab1a5e0506405b0c7aefc327a0))

## [0.7.5](https://github.com/Naroh091/PideInfo-Agent/compare/v0.7.4...v0.7.5) (2026-05-20)


### Bug Fixes

* **windows:** skip FF lock files when copying profiles ([4a07af6](https://github.com/Naroh091/PideInfo-Agent/commit/4a07af65c551a7381a792495e4171f2cb25514ac))

## [0.7.4](https://github.com/Naroh091/PideInfo-Agent/compare/v0.7.3...v0.7.4) (2026-05-18)


### Bug Fixes

* fix default URI, pasting token ([2987d13](https://github.com/Naroh091/PideInfo-Agent/commit/2987d134139edf062bb4808be13a0f7e1a6cebb1))

## [0.7.3](https://github.com/Naroh091/PideInfo-Agent/compare/v0.7.2...v0.7.3) (2026-05-18)


### Bug Fixes

* avoid sync if token has not been provided ([28155cd](https://github.com/Naroh091/PideInfo-Agent/commit/28155cdc1affe9740b2eef68558d0e5f246bc0ec))
* manage UI in a dedicated thread ([d7a02a3](https://github.com/Naroh091/PideInfo-Agent/commit/d7a02a32662599f87568920dec8643e04792a9ff))

## [0.7.2](https://github.com/Naroh091/PideInfo-Agent/compare/v0.7.1...v0.7.2) (2026-05-18)


### Bug Fixes

* skip REG step 3 attachments — request body already in EXPONE/SOL… ([9124df2](https://github.com/Naroh091/PideInfo-Agent/commit/9124df2ca0cec1becd80c46ceb70f699ad13fb43))
* skip REG step 3 attachments — request body already in EXPONE/SOLICITA ([dd6e114](https://github.com/Naroh091/PideInfo-Agent/commit/dd6e114739fd694be82ac8fd54f8361d6c78db79))

## [0.7.1](https://github.com/Naroh091/PideInfo-Agent/compare/v0.7.0...v0.7.1) (2026-05-17)


### Bug Fixes

* handle request UUID when sending REG ([ac24b63](https://github.com/Naroh091/PideInfo-Agent/commit/ac24b63f14932de3e08a0c4354755d96b1c8a907))

## [0.7.0](https://github.com/Naroh091/PideInfo-Agent/compare/v0.6.3...v0.7.0) (2026-05-17)


### Features

* implement full records ([27b5e5e](https://github.com/Naroh091/PideInfo-Agent/commit/27b5e5e609653e82a4d771082db7970cfe9f5648))

## [0.6.3](https://github.com/Naroh091/PideInfo-Agent/compare/v0.6.2...v0.6.3) (2026-05-16)


### Bug Fixes

* download confirmation after REG process ([768bf04](https://github.com/Naroh091/PideInfo-Agent/commit/768bf047e5fbd6d973afbbf2606898113d5c6a7f))
* REG justificante download retries when SARA returns "URL no existe" ([e997604](https://github.com/Naroh091/PideInfo-Agent/commit/e997604457a159ba21293bad270a0de2b9745f6d))
* REG justificante downloads via API instead of UI popup-prone button ([0e697ff](https://github.com/Naroh091/PideInfo-Agent/commit/0e697ffb4e0afcfc670740544870328b84387415))

## [0.6.2](https://github.com/Naroh091/PideInfo-Agent/compare/v0.6.1...v0.6.2) (2026-05-16)


### Bug Fixes

* REG justificante upload uses canonical client method ([47be977](https://github.com/Naroh091/PideInfo-Agent/commit/47be9778c3b17d46b2df7d7b4c76108933f956e7))
* REG justificante upload uses canonical client method ([55211ac](https://github.com/Naroh091/PideInfo-Agent/commit/55211ac75167400f9494ca73a11e9a3c53839151))

## [0.6.1](https://github.com/Naroh091/PideInfo-Agent/compare/v0.6.0...v0.6.1) (2026-05-16)


### Bug Fixes

* REG destination picker focuses real combobox input ([3e4885e](https://github.com/Naroh091/PideInfo-Agent/commit/3e4885e253d4bfbf65857582ce60728146ee2c06))
* REG destination picker focuses real combobox input ([d1bd45e](https://github.com/Naroh091/PideInfo-Agent/commit/d1bd45ebea83954d5df77ccba29cab96d9b9a4bf))

## [0.6.0](https://github.com/Naroh091/PideInfo-Agent/compare/v0.5.0...v0.6.0) (2026-05-15)


### Features

* REG ([6522d39](https://github.com/Naroh091/PideInfo-Agent/commit/6522d39f4ab729283692979b7045a672c0ac834a))
* REG ([885b17a](https://github.com/Naroh091/PideInfo-Agent/commit/885b17a93d55b2715e061d5ff0333591a1160ca5))


### Bug Fixes

* reg implementation ([e848b55](https://github.com/Naroh091/PideInfo-Agent/commit/e848b55c5c5384206b9e62ac89296c3dbdea871c))

## [0.5.0](https://github.com/Naroh091/PideInfo-Agent/compare/v0.4.0...v0.5.0) (2026-05-11)


### Features

* **ctbg:** derive complaint branch/reason from resolution_result ([212a30d](https://github.com/Naroh091/PideInfo-Agent/commit/212a30de1b6d2a4e24306b07e1059ba7830f2462))

## [0.4.0](https://github.com/Naroh091/PideInfo-Agent/compare/v0.3.1...v0.4.0) (2026-05-04)


### Features

* add notifications when sending requests ([5c400b9](https://github.com/Naroh091/PideInfo-Agent/commit/5c400b9f6eabd16fea1b19a91cd31c1b6696e0a0))


### Bug Fixes

* CTBG filler ([9ea3688](https://github.com/Naroh091/PideInfo-Agent/commit/9ea3688d0216097e4701f947524cb9bf93d97639))
* detect stale Firefox version and re-download on startup ([2b914ae](https://github.com/Naroh091/PideInfo-Agent/commit/2b914aeecf3deaf62ed6e678e66c48b0389c6626))

## [0.3.1](https://github.com/Naroh091/PideInfo-Agent/compare/v0.3.0...v0.3.1) (2026-05-03)


### Bug Fixes

* force release ([73c6f0b](https://github.com/Naroh091/PideInfo-Agent/commit/73c6f0b399b434167225da7607e92aa62717a437))

## [0.3.0](https://github.com/Naroh091/PideInfo-Agent/compare/v0.2.0...v0.3.0) (2026-05-02)


### Features

* implement sending access requests through the Portal de Transparencia ([48fbd8e](https://github.com/Naroh091/PideInfo-Agent/commit/48fbd8ec1892089d14f0f3f3def2391636f6acfa))

## [0.2.0](https://github.com/Naroh091/PideInfo-Agent/compare/v0.1.0...v0.2.0) (2026-05-01)


### Features

* add sentry to the agent ([e79a3fd](https://github.com/Naroh091/PideInfo-Agent/commit/e79a3fdbf434f477512b9924c90a12ff4feb2a5a))
* agent ([0bbf6cc](https://github.com/Naroh091/PideInfo-Agent/commit/0bbf6cc05376e0d10c314c6b9af73314cec428c1))
* agent first implementation ([c8c6461](https://github.com/Naroh091/PideInfo-Agent/commit/c8c64616cd62e8bfcb2dc8fdf002da3acf71f428))
* implement DEHu ([1005cfd](https://github.com/Naroh091/PideInfo-Agent/commit/1005cfd6874dc9b21bb6894c7a0df06c27a1219a))
* implement handler in MacOS ([48c0d89](https://github.com/Naroh091/PideInfo-Agent/commit/48c0d8958f25a56ae71a9b5cec2c6b169372b016))
* implement REG SARA ([f3f1f8d](https://github.com/Naroh091/PideInfo-Agent/commit/f3f1f8d9b3e9168596e18de70919072553df0c26))
* implement warnings on pending notifications ([5ed5cfa](https://github.com/Naroh091/PideInfo-Agent/commit/5ed5cfa54762634c12900f03f93f985b6009bda7))
* import CTBG expedients, AI summary, big redesign ([819fa18](https://github.com/Naroh091/PideInfo-Agent/commit/819fa18e9c468d7e70dcb402e64922671e9b7dc3))
* JWT ([86a8197](https://github.com/Naroh091/PideInfo-Agent/commit/86a81972087dcfdcb38097a4c3e51828ea389ee8))
* JWT for agents ([034729e](https://github.com/Naroh091/PideInfo-Agent/commit/034729eb06ade0fb06ac79f192be8e057228cfb6))
* register complaints in the CTBG ([c2eaeb0](https://github.com/Naroh091/PideInfo-Agent/commit/c2eaeb0d1f503577399928a587d73ba772230dfe))
* register complaints in the CTBG ([d6d7ef1](https://github.com/Naroh091/PideInfo-Agent/commit/d6d7ef100d028979c5c8c9fe688c925e124fa453))


### Bug Fixes

* **agent:** tray and daemon as default behavior for agent ([2c08d6e](https://github.com/Naroh091/PideInfo-Agent/commit/2c08d6e54a88c80ea8605c607af84a20ead448c9))
* CTBG form ([2eb7785](https://github.com/Naroh091/PideInfo-Agent/commit/2eb77858141a8ae6827be3e343970a051c3cf096))
* date coherence between portals ([11b36fd](https://github.com/Naroh091/PideInfo-Agent/commit/11b36fd244258bf8cf613fc83320c2c836c499d3))
* JWT extraction in DEHu ([36b154c](https://github.com/Naroh091/PideInfo-Agent/commit/36b154c5209af82b2a1f63b4b3fb519c59532dce))
* make agent work with CTBG ([9902ba9](https://github.com/Naroh091/PideInfo-Agent/commit/9902ba9cbf7632ea9772743b4f05175b61630e67))
* race condition in ctbg form ([f3f6ef1](https://github.com/Naroh091/PideInfo-Agent/commit/f3f6ef18b6ab9def957836505f462033114c5dc2))
* verify Firefox is available ([31be979](https://github.com/Naroh091/PideInfo-Agent/commit/31be97945419f25212fc76eefba57ad673156f35))
