# Downloaded from https://github.com/autopkg/hansen-m-recipes/blob/de17d7db2537ffa00c27281a392f37fe599efdca/SharedProcessors/README.md
# Commit: de17d7db2537ffa00c27281a392f37fe599efdca
# Downloaded at: 2025-11-27 20:29:07 UTC

These SharedProcessors are for use in AutoPkg recipies:

## General Processors

### CopyImporter.py
- Intended to be used for importing new software into a software distribution by copying source files to another destination
- Based on the built-in Copier.py, it copies a source file (but not a directory) to a destination, only overwriting if `overwrite=True`
- Provides `copyimporter_summary_result` for [Processor Summary Reporting](https://github.com/autopkg/autopkg/wiki/Processor-Summary-Reporting)

## Processors for Windows Software

### BESRelevanceProvider.py
- Uses the BigFix QnA tool to parse files for version info
- Prereq: Run `autopkg install QnA`

### HachoirMetaDataProvider.py
- Uses the Hachoir MetaData python library to get version information from EXE files
- Prereq: run `pip install hachoir_core hachoir_metadata hachoir_parser`

### MSIVersionProvider.py
- ~~Uses the wine and lessmsi application and wine to get version information from MSI files.~~ Depreciated 02/2020

### MSIInfoVersionProvider.py
- Uses the msitools and misinfo tool to get version information from MSI files https://wiki.gnome.org/msitools
- Prereq: run `brew install msitools`

### WinInstallerExtractor.py
- Uses the 7zip library to extract files (EXEs) to get their contents for further processing, including parsing the resulting files for version information
- Prereq: run `brew install p7zip`

### GoogleChromeWinVersioner.py
- Uses 7zip (/usr/local/bin/7z) library to extract Google Chrome MSI and regex to pull the current version from the SummaryInformation file
- Prereq: run `brew install p7zip`
