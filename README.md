# Aixact AI – Verification Workspace Backend

Aixact AI is a backend service that powers a fact-checking and verification workspace.  
It exposes HTTP APIs to analyze user-submitted claims using a custom Free Fact Checker pipeline and supports a contact form for your site.

## Features

- **Free Fact Checker engine**
  - Uses NLP (tokenization, stemming, stopword removal)
  - Common-sense and nationality-aware knowledge base
  - Web evidence retrieval and readability extraction
  - Truth scoring with clear TRUE/FALSE style verdicts and explanations

- **Content extraction**
  - Fetches and parses web pages
  - Uses Mozilla Readability and jsdom + cheerio to extract readable article text

- **API endpoints**
  - `/api/analyze` – Analyze a claim and return verdict, confidence, evidence, and explanation
  - `/api/contact` – Receive contact form submissions and send them via email (with SMTP support)

- **Production-ready touches**
  - Environment-driven configuration
  - Graceful fallback when headless rendering or SMTP is not configured

## Tech Stack

- **Runtime:** Node.js
- **Framework:** Express
- **Language:** JavaScript (CommonJS)
- **Core libraries:**
  - `natural` – NLP utilities (tokenizer, sentiment)
  - `stopword` – Stopword removal
  - `apos-to-lex-form` – Text normalization
  - `node-fetch` – HTTP requests
  - `cheerio`, `jsdom`, `@mozilla/readability` – HTML parsing & article extraction
  - `nodemailer` – Email sending
  - `playwright-core` (optional) – Headless browser support for advanced fetching

## Project Structure

- **`server.js`**  
  Main Express server:
  - Serves static files from the project root (including `index.html`)
  - Defines `/api/analyze` for claim analysis
  - Defines `/api/contact` for contact form handling
  - Configures mail transport if SMTP env vars are present

- **`freeFactCheck.js`**  
  Implements the `FreeFactChecker` class:
  - Text preprocessing and sentiment analysis
  - Common-sense fact rules and nationality knowledge base
  - Web search & article text extraction
  - Truth scoring and verdict generation

- **`index.html`** (and other static assets)  
  Frontend files served directly by Express via `express.static(__dirname)`.

- **`package.json`**  
  Project metadata, dependencies, and npm scripts.

## Getting Started

### Prerequisites

- **Node.js** (LTS recommended)
- **npm** (comes with Node)

### Installation

1. Clone or download this repository.
2. Install dependencies.

Authors: Srawan Pandey , Shreya Srivastava.
Email: srawanp14@gmail.com , shreyasrivastava090807@gmail.com.
Project: Aixact AI – Detect. Verify. Trust.
