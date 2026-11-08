# Banners

Style: light "book page". The background uses the engraving's own paper colour
(`#fefeca`), the text is near-black and the accent is an ink rust (`#9a3a17`). The
engraving sits directly on the background without a frame.

- `../social-preview.png` (1280×640) is the GitHub social preview image. Upload it under
  **Settings → General → Social preview**.
- `../header.jpg` (1280×400, rendered at 2× = 2560×800) is the header at the top of the
  README.

To regenerate them:

```sh
cd docs/banner
npm i @fontsource/fraunces@5.1.0 @fontsource/inter@5.1.0 playwright
# optional: re-derive the engraving with white paper, which multiplies cleanly onto the
# beige background (#fefeca, the paper colour of the original)
convert EB9_Shrew.jpg -channel R -level 0,96% -channel G -level 0,96% \
        -channel B -level 0,77% +channel -level 0,97% shrew-light.png
node render.js banner.html ../social-preview.png 1280 640 1
node render.js header.html header.png 1280 400 2 && convert header.png -quality 92 -sampling-factor 4:4:4 ../header.jpg
```

The illustration is `EB9_Shrew.jpg`, the common shrew (*Sorex araneus*, "*Sorex vulgaris*"
in the original). Engraving by C. Butterworth, *Encyclopædia Britannica*, 9th ed.,
vol. XXI, p. 843 (1886). It is in the **public domain** and comes from
[Wikimedia Commons](https://commons.wikimedia.org/wiki/File:EB9_Shrew.jpg).
