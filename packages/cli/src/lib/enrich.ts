import https from "https"; // FILTRO DE YOUTUBE SHORTS
import cheerio from "cheerio";
import { performance } from "perf_hooks";
import Parser from "rss-parser";
import { downloadTextFile } from "../utils/download";
import { getFirstNonNullItem } from "../utils/get-first-non-null-item";
import { htmlToText } from "../utils/html-to-text";
import { trimWithThreshold } from "../utils/trim-with-threshold";
import type { Cache } from "./get-cache";
import type { Config, Source } from "./get-config";
import { normalizeFeed, ParsedFeedItem } from "./normalize-feed";

const MILLISECONDS_PER_DAY = 86400000;
const SUMMARY_TRIM_ACTIVATION_THRESHOLD = 2048;
const SUMMARY_TRIM_TO_LENGTH = 800;

export interface EnrichedArticle {
  id: string;
  author: string | null;
  description: string;
  link: string;
  publishedOn: string;
  title: string;
  wordCount: number | null;
  imageUrl: string | null;
  enclosure?: { url: string; type: string; length: number };
  itunes?: { duration?: string };
}

export interface EnrichedSource {
  title: string | null;
  feedUrl: string;
  siteUrl: string | null;
  articles: EnrichedArticle[];
}

const parser = new Parser({
  customFields: {
    item: ["media:thumbnail", "media:content"],
  },
});

export interface EnrichInput {
  source: Source;
  cache: Cache;
  config: Config;
}

/** @returns null when enrich failed due to fatal errors */
export async function enrich(enrichInput: EnrichInput): Promise<EnrichedSource | null> {
  return enrichInternal(enrichInput);
}

async function enrichInternal(enrichInput: EnrichInput): Promise<EnrichedSource | null> {
  const { source, cache, config } = enrichInput;
  const cachedSource = cache.sources.find((entry) => entry.feedUrl === source.href);
  const startTime = performance.now();

  const xmlString = await downloadTextFile(source.href).catch(() => {
    console.error(`[enrich] Error downloading source ${source.href}`);
    return null;
  });

  if (!xmlString) {
    if (cachedSource) {
      console.log(`[enrich] Error recovery: cache used for ${source.href}`);
      return cachedSource;
    }
    console.log(`[enrich] Error recovery: no cache available. ${source.href} is skipped.`);
    return null;
  }

  const rawFeed = await parser.parseString(xmlString).catch((err) => {
    console.error(`[enrich] Parse source failed ${source.href}`);
    throw err;
  });
  const feed = normalizeFeed(rawFeed, source.href);
  const now = Date.now();
  const cachedArticles = cachedSource?.articles ?? [];
  const cachedByLink = new Map(cachedArticles.map((article) => [article.link, article]));

  // Refresh metadata for articles already in cache using the current RSS item.
  // Keep cached enrichment only as fallback when the feed omits a field.
  const refreshedCachedArticles: EnrichedArticle[] = [];
  const itemsToEnrich: ParsedFeedItem[] = [];

  for (const item of feed.items) {
    if (!item.link) continue;
    const cached = cachedByLink.get(item.link);
    if (!cached) {
      itemsToEnrich.push(item);
      continue;
    }

    const feedDescription = getSummary({ parsedItem: item, enrichedItem: unenrichableItem });
    refreshedCachedArticles.push({
      ...cached,
      id: item.guid ?? cached.id,
      author: item.creator ?? cached.author,
      title: item.title ?? cached.title,
      description: feedDescription || cached.description,
      publishedOn: item.isoDate ?? cached.publishedOn,
      imageUrl: item.imageUrl ?? cached.imageUrl,
      enclosure: item.enclosure ?? cached.enclosure,
      itunes: item.itunes ?? cached.itunes,
    });
  }

  const newArticlesAsync: Promise<EnrichedArticle | null>[] = itemsToEnrich.map(async (item) => {
    const title = item.title ?? "Untitled";
    const link = item.link;
    if (!link) return null;

    // Filtro de YouTube Shorts: mantém o comportamento existente.
    if (link.includes("youtube.com") || link.includes("youtu.be")) {
      if (link.includes("/shorts/")) {
        console.log(`[enrich] YouTube Short ignorado: ${title}`);
        return null;
      }
      const videoIdMatch = link.match(/(?:v=|youtu\.be\/)([^&?]+)/);
      if (videoIdMatch?.[1]) {
        const videoId = videoIdMatch[1];
        const isShort = await new Promise<boolean>((resolve) => {
          https.request(`https://www.youtube.com/shorts/${videoId}`, { method: "HEAD" }, (res) =>
            resolve(res.statusCode === 200)
          ).on("error", () => resolve(false)).end();
        });
        if (isShort) {
          console.log(`[enrich] YouTube Short ignorado: ${title}`);
          return null;
        }
      }
    }

    const enrichedItem = isItemEnrichable(item) ? await enrichItem(link) : unenrichableItem;
    return {
      id: item.guid ?? link,
      author: item.creator ?? null,
      description: getSummary({ parsedItem: item, enrichedItem }),
      link,
      publishedOn: item.isoDate ?? enrichedItem.publishedTime?.toISOString() ?? new Date().toISOString(),
      wordCount: enrichedItem.wordCount,
      title,
      itunes: item.itunes,
      enclosure: item.enclosure,
      imageUrl: item.imageUrl ?? enrichedItem.imageUrl ?? null,
    };
  });

  const newArticles = (await Promise.all(newArticlesAsync)).filter(
    (article): article is EnrichedArticle => article !== null
  );

  const currentFeedLinks = new Set(feed.items.map((item) => item.link).filter((link): link is string => !!link));
  const articlesNoLongerInFeed = cachedArticles.filter((article) => !currentFeedLinks.has(article.link));
  const combinedArticles = [...newArticles, ...refreshedCachedArticles, ...articlesNoLongerInFeed];

  const renderedArticles = combinedArticles
    .filter((item) => {
      const publishedTime = new Date(item.publishedOn).getTime();
      if (!Number.isFinite(publishedTime)) return false;
      const elapsedDate = Math.round((now - publishedTime) / MILLISECONDS_PER_DAY);
      return elapsedDate < config.cacheMaxDays && elapsedDate >= 0;
    })
    .sort((a, b) => b.publishedOn.localeCompare(a.publishedOn));

  const durationInSeconds = ((performance.now() - startTime) / 1000).toFixed(2);
  console.log(
    `[enrich] ${durationInSeconds.padStart(4)}s | ${cachedArticles.length.toString().padStart(3)} cached | ${refreshedCachedArticles.length
      .toString().padStart(3)} refreshed | ${newArticles.length.toString().padStart(3)} new | ${source.href}`
  );

  return {
    title: feed.title ?? cachedSource?.title ?? null,
    feedUrl: source.href,
    siteUrl: feed.link ?? cachedSource?.siteUrl ?? null,
    articles: renderedArticles,
  };
}

export interface EnrichItemResult {
  description: string | null;
  wordCount: number | null;
  publishedTime: Date | null;
  imageUrl: string | null;
}

async function enrichItem(link: string): Promise<EnrichItemResult> {
  try {
    const responseHtml = await downloadTextFile(link);
    const $ = cheerio.load(responseHtml);
    const wordCount = $.root().text().split(/\s+/).length;

    let description = $(`meta[property="og:description"]`).attr("content") ?? null;
    if (!description?.length) description = $(`meta[name="twitter:description"]`).attr("content") ?? null;
    if (!description?.length) description = $(`meta[name="description"]`).attr("content") ?? null;
    description = description?.length ? htmlToText(description) : null;

    let publishedTime: Date | null = null;
    const publishedTimeString = $(`meta[property="article:published_time"]`).attr("content") ?? null;
    if (publishedTimeString) {
      const parsedDate = new Date(publishedTimeString);
      if (Number.isFinite(parsedDate.getTime())) publishedTime = parsedDate;
    }

    return {
      description,
      wordCount,
      publishedTime,
      imageUrl: $(`meta[property="og:image"]`).attr("content") ?? null,
    };
  } catch {
    console.log(`[enrich] Error enrich ${link}; RSS data retained`);
    return unenrichableItem;
  }
}

const unenrichableItem: EnrichItemResult = {
  description: null,
  wordCount: null,
  publishedTime: null,
  imageUrl: null,
};

function getSummary(input: GetSummaryInput): string {
  return getFirstNonNullItem(
    input.parsedItem.summary,
    input.parsedItem.content
      ? trimWithThreshold(input.parsedItem.content, SUMMARY_TRIM_ACTIVATION_THRESHOLD, SUMMARY_TRIM_TO_LENGTH)
      : null,
    input.enrichedItem.description,
    ""
  );
}

interface GetSummaryInput {
  parsedItem: ParsedFeedItem;
  enrichedItem: EnrichItemResult;
}

const NO_ENRICH_URL_PATTERNS = ["^https?://www.youtube.com/watch"];

function isItemEnrichable(item: ParsedFeedItem): boolean {
  if (!item.link || item.itunes) return false;
  return !NO_ENRICH_URL_PATTERNS.some((pattern) => item.link!.match(pattern));
}
