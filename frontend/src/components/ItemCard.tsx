import { useId, useState } from 'react';

import type { FeedItem } from '../api/types';
import { formatAbsolute, formatRelative, hostOf } from '../lib/format';
import { BookmarkIcon, CheckIcon, FilterIcon } from './icons';

export interface ItemCardActions {
  onOpen: (item: FeedItem) => void;
  onToggleRead: (item: FeedItem) => void;
  onToggleBookmark: (item: FeedItem) => void;
  /** Narrow the feed to one source. Absent on screens without filters. */
  onFilterSource?: (slug: string) => void;
  onFilterTopic?: (slug: string) => void;
  /** Bookmarks screen shows an explicit remove instead of a toggle. */
  onRemoveBookmark?: (item: FeedItem) => void;
}

interface ItemCardProps extends ItemCardActions {
  item: FeedItem;
  /**
   * Roving tabindex over the list: exactly one card carries 0 and every
   * other carries -1, so Tab reaches the list in one press and leaves it in
   * one, and `j`/`k` move within it. Absent on any screen with no keyboard
   * layer, where the card stays unfocusable.
   */
  tabIndex?: number;
}

export function ItemCard({
  item,
  onOpen,
  onToggleRead,
  onToggleBookmark,
  onFilterSource,
  onFilterTopic,
  onRemoveBookmark,
  tabIndex,
}: ItemCardProps) {
  /**
   * Artwork, in order of preference, skipping anything the browser has
   * already failed to load.
   *
   * Two candidates rather than one, because most items have no image of
   * their own and the source almost always does — Hacker News, Lobsters
   * and LWN publish an item image about never, so before this the great
   * majority of cards were a headline and a rule.
   *
   * Failures are tracked by URL rather than as a single boolean, because
   * a chain needs to know *which* link broke: a dead item image must fall
   * through to the channel's, and a dead channel image must leave the
   * card without artwork rather than retrying the one that already
   * failed.
   */
  const [failed, setFailed] = useState<readonly string[]>([]);
  const itemImage = item.image_url && !failed.includes(item.image_url) ? item.image_url : null;
  const channelImage =
    item.source.icon_url && !failed.includes(item.source.icon_url) ? item.source.icon_url : null;
  const artwork = itemImage ?? channelImage;
  const titleId = useId();
  const readFlagId = `${titleId}-read`;

  return (
    <article
      className="card"
      data-read={item.read ? 'true' : 'false'}
      // How the keyboard layer finds this card to focus it. Kept on the
      // element that takes focus, not on the <li>, so the two cannot drift.
      data-card-id={item.id}
      tabIndex={tabIndex}
      // Named by its own title, so landing here with `j` announces the
      // headline rather than "article". Read state joins the label by
      // pointing at the flag that is already on screen — no second copy of
      // the word to fall out of step, and nothing invented for the label
      // that a sighted user cannot also see.
      aria-labelledby={item.read ? `${titleId} ${readFlagId}` : titleId}
    >
      {artwork ? (
        // The fallback is a different kind of picture and is not styled as
        // the same one: a channel logo is square-ish and branded, so
        // `cover` at the item image's height crops it to a slice of
        // gradient. The modifier gives it `contain` in a shorter box —
        // legible as a mark rather than passed off as a photograph.
        <img
          className={itemImage ? 'card__image' : 'card__image card__image--channel'}
          src={artwork}
          alt=""
          loading="lazy"
          decoding="async"
          referrerPolicy="no-referrer"
          onError={() => {
            setFailed((current) =>
              current.includes(artwork) ? current : [...current, artwork],
            );
          }}
        />
      ) : null}

      <h3 className="card__title" id={titleId}>
        <a
          className="card__link"
          // The shortcut layer opens an item by clicking this link rather
          // than reaching for the URL itself, so `o` takes the identical
          // path a mouse click takes — same target, same rel, same
          // mark-as-read — and the two cannot diverge.
          data-card-link=""
          href={item.canonical_url}
          target="_blank"
          rel="noopener noreferrer"
          onClick={() => {
            onOpen(item);
          }}
          onAuxClick={(event) => {
            // Middle-click opens in a background tab; still a read.
            if (event.button === 1) onOpen(item);
          }}
        >
          {item.title}
        </a>
      </h3>

      {item.summary ? <p className="card__summary">{item.summary}</p> : null}

      <div className="card__meta">
        {onFilterSource ? (
          <button
            type="button"
            className="card__source"
            onClick={() => {
              onFilterSource(item.source.slug);
            }}
            title={`Show only ${item.source.name}`}
          >
            <SourceIcon item={item} />
            <span>{item.source.name}</span>
            <FilterIcon />
          </button>
        ) : (
          <span className="card__source card__source--static">
            <SourceIcon item={item} />
            <span>{item.source.name}</span>
          </span>
        )}

        <time className="card__time" dateTime={item.published_at} title={formatAbsolute(item.published_at)}>
          {formatRelative(item.published_at)}
        </time>

        {item.read ? (
          <span className="card__read-flag" id={readFlagId}>
            Read
          </span>
        ) : null}
      </div>

      {item.topics.length > 0 ? (
        <ul className="card__topics">
          {item.topics.map((slug) => (
            <li key={slug}>
              {onFilterTopic ? (
                <button
                  type="button"
                  className="tag tag--button"
                  onClick={() => {
                    onFilterTopic(slug);
                  }}
                  title={`Show only ${slug}`}
                >
                  {slug}
                </button>
              ) : (
                <span className="tag">{slug}</span>
              )}
            </li>
          ))}
        </ul>
      ) : null}

      <div className="card__actions">
        {onRemoveBookmark ? (
          <button
            type="button"
            className="button button--quiet"
            onClick={() => {
              onRemoveBookmark(item);
            }}
          >
            <BookmarkIcon filled />
            Remove bookmark
          </button>
        ) : (
          <button
            type="button"
            className="button button--quiet"
            aria-pressed={item.bookmarked}
            onClick={() => {
              onToggleBookmark(item);
            }}
          >
            <BookmarkIcon filled={item.bookmarked} />
            {item.bookmarked ? 'Bookmarked' : 'Bookmark'}
          </button>
        )}

        <button
          type="button"
          className="button button--quiet"
          aria-pressed={item.read}
          onClick={() => {
            onToggleRead(item);
          }}
        >
          <CheckIcon />
          {item.read ? 'Mark unread' : 'Mark read'}
        </button>

        <span className="card__host">{hostOf(item.canonical_url)}</span>
      </div>
    </article>
  );
}

function SourceIcon({ item }: { item: FeedItem }) {
  if (!item.source.icon_url) return null;
  return (
    <img
      className="card__source-icon"
      src={item.source.icon_url}
      alt=""
      width={16}
      height={16}
      loading="lazy"
      decoding="async"
      referrerPolicy="no-referrer"
    />
  );
}
