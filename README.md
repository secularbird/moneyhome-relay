# moneyhome-relay

Relays two public RSS feeds (南华早报 SCMP, Nikkei Asia) that a server inside mainland China cannot reach.
A scheduled GitHub Action fetches them and publishes compact JSON (title, link, publication time, a
160-character summary) to the `data` branch; a private dashboard reads it from
`https://raw.githubusercontent.com/<owner>/moneyhome-relay/data/<file>.json`.

The text belongs to the original publishers; this repository only carries headlines, links and a short
excerpt so a personal dashboard can show them. Nothing here is a secret and no credentials are used.
