<div align="center">

  # mov-cli-archive
  <sub>A mov-cli v4 plugin for browsing and streaming from the Internet Archive (archive.org).</sub>

</div>

## Features
- **Stream Public Video & Audio:** Watch movies, anime, and classic television, or listen to music, old-time radio, podcasts, audiobooks, and live concerts directly from archive.org.
- **Search Audio, Video, or Both:** Easily target audio (`audio:query`), video (`video:query`), or search across both simultaneously (`both:query`).
- **Image Previews & Posters:** Automatically retrieves item poster thumbnails from Archive.org's image service (`https://archive.org/services/img/{id}`) to render previews directly in your terminal using `chafa` (or Kitty terminal `icat`).
- **Interactive Multi-Media Catalog:** Run `mov-cli -s archive ""` to choose between Video, Audio, or Both, and browse curated collections (Anime, Feature Films, Old Time Radio, Live Music Archive, Podcasts, etc.).
- **Nested Searching:** After selecting a collection in the catalog, you can optionally search specifically within that collection.
- **Direct Collection Filters:** Search collections directly from your terminal: `mov-cli -s archive "collection:anime-series haruhi"` or `mov-cli -s archive "audio:collection:oldtimeradio shadow"`
- **Authentication:** Supports logging in to access restricted content.
- **Broad Search:** Uses Archive's Lucene search backend. You can use standard text searches, exact quotes (`"phrase"`), or wildcards (`*`).
- **Caching & Validation:** In-memory TTL caching and field-limiting queries for instant navigation and minimal network payload.

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
options = { limit = 100, fetch_images = true, media_type = "video" }
```

### Enabling Terminal Image Previews (via fzf & chafa)
`mov-cli` has built-in image preview support when `fzf` and `preview` are enabled. To view item poster thumbnails in your terminal alongside search results:

In `~/.config/mov-cli/config.toml`:
```toml
[mov-cli.ui]
fzf = true
preview = true
```

Ensure `chafa` is installed on your system (e.g. `sudo apt install chafa`). When you search with `mov-cli`, `fzf` will display the thumbnail image for each highlighted Archive item in the preview pane.

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
*   `fetch_images`: Set to `true` (default) to attach poster image URLs to items, or `false` to disable.
*   `media_type`: Default search target (`"video"`, `"audio"`, or `"both"`). Defaults to `"video"`.

## Usage

**Search Videos (Default):**
```sh
mov-cli -s archive "matrix"
```

**Search Audio (Music, OTR, Podcasts, Soundtracks):**
```sh
mov-cli -s archive "audio:zelda"
# or
mov-cli -s archive "type:audio beethoven"
```

**Search Both Video and Audio Simultaneously:**
```sh
mov-cli -s archive "both:evangelion"
# or
mov-cli -s archive "type:both final fantasy"
```

**Open the interactive catalog (select between Video, Audio, or Both):**
```sh
mov-cli -s archive ""
```

**Search within a specific collection:**
```sh
mov-cli -s archive "collection:Comedy_Films"
mov-cli -s archive "audio:collection:oldtimeradio shadow"
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
