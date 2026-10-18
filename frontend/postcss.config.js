module.exports = {
  plugins: {
    '@tailwindcss/postcss': {},
    // Tailwind v4 prefixes only what it generates. The stylesheets in
    // src/styles/ still need it, notably on the dev server, which the cloud
    // image serves and which does not minify.
    autoprefixer: {},
  },
}
