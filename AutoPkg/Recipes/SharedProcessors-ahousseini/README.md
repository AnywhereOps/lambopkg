# Downloaded from https://github.com/autopkg/ahousseini-recipes/blob/31e6138b990db5622ef2f3ab05d541edcf92997e/SharedProcessors/README.md
# Commit: 31e6138b990db5622ef2f3ab05d541edcf92997e
# Downloaded at: 2025-11-27 22:30:47 UTC

# Shared Processors

To use these processors, add the processor like this:

```xml
com.github.ahousseini-recipes.SharedProcessors/SharedProcessor
```

## HomebrewCaskURL

### Description

An AutoPkg processor which reads the download url from the
[Homebrew Cask API](https://formulae.brew.sh/docs/api/).

### Input Variables

- **cask\_name:**
  - **required:** True
  - **description:** Name of cask to fetch, as would be given to the `brew`
    command. Example: `brew install --cask firefox`.

### Output Variables

- **url:**
  - **description:** URL for the Cask's download.
