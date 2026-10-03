<div align="center">

  # mov-cli-archive
  <sub>A mov-cli v4 plugin for browsing and streaming from the Internet Archive (archive.org).</sub>

</div>

## Features
- **Stream Public Content:** Watch movies, anime, and classic television directly from archive.org.
- **Interactive Catalog:** Run `mov-cli -s archive ""` to explore a curated list of top collections (e.g., Anime, Sci-Fi, Feature Films).
- **Nested Searching:** After selecting a collection in the catalog, you can optionally search specifically within that collection.
- **Direct Collection Filters:** Search collections directly from your terminal: `mov-cli -s archive "collection:anime-series haruhi"`
- **Authentication:** Supports logging in to access restricted content.
- **Broad Search:** Uses Archive's Lucene search backend. You can use standard text searches, exact quotes (`"phrase"`), or wildcards (`*`).

## Installation

Since mov-cli is typically installed globally via `pipx`, you can inject this plugin directly into its environment:

```sh
pipx inject mov-cli /path/to/mov-cli-archive-plugin
```

If mov-cli is already installed and you are using pipx
```sh
pipx inject mov-cli git+https://github.com/salvitch/mov-cli-internet-archive.git
```

## Configuration

Add the plugin to your `mov-cli` configuration file (usually `~/.config/mov-cli/config.toml`). 

```toml
[mov-cli.plugins]
archive = "mov-cli-archive"

[mov-cli.scrapers.archive]
namespace = "archive.DEFAULT"
options = { limit = 100 }
```

### Authentication (Optional)
If you want to access borrowed or restricted content, you need to authenticate. There are two ways to do this:

**1. Secure Method (Recommended):**
Do not put your credentials in the `config.toml`. Instead, use the official `internetarchive` command-line tool to securely save your login session to `~/.config/ia.ini`. If you installed this in a `pipx` environment, you can run:
```sh
pipx run --spec internetarchive ia configure
```
Once configured, the plugin will automatically detect and use your secure session!

**2. TOML Config Method:**
Alternatively, you can place them directly in your `config.toml` options (not recommended for security):
`options = { username = "your_email", password = "your_password", limit = 100 }`

### Options Explained:
*   `limit`: The maximum number of search results to fetch. Set to `100` by default. Set to `0` for unlimited results.

## Usage

**Search globally:**
```sh
mov-cli -s archive "matrix"
```

**Open the interactive catalog:**
```sh
mov-cli -s archive ""
```

**Search within a specific collection:**
```sh
mov-cli -s archive "collection:Comedy_Films"
```

**Search for a keyword within a specific collection:**
```sh
mov-cli -s archive "collection:anime-series evangelion"
```

### Advanced Search Syntax (Lucene)
Archive.org uses Lucene search syntax. Since this plugin searches broadly across all metadata, you can use powerful search operators:

*   **Broad Search (Default):** Searching `anime clips` will look for both words anywhere in the item's title, description, or tags.
*   **Exact Phrases:** Wrap your search in quotes for exact matches: `"anime clips"`
*   **Wildcards:** Use an asterisk `*` for partial matches. Searching `haruh*` will match Haruhi, Haruha, etc.
*   **Exclusions:** Use a minus sign `-` to exclude terms. Searching `anime -dub` will find anime but filter out anything containing the word "dub".
