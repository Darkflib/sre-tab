/**
 * Convenience aliases over the generated schema. Every type here is
 * derived from `openapi.json` — nothing is hand-written, so a contract
 * change shows up as a type error rather than a runtime surprise.
 */
import type { components } from './schema';

type Schemas = components['schemas'];

export type FeedItem = Schemas['FeedItemOut'];
export type FeedPage = Schemas['FeedPage'];
export type FeedSourceRef = Schemas['FeedSourceRef'];
export type BookmarkEntry = Schemas['BookmarkOut'];
export type BookmarkPage = Schemas['BookmarkPage'];
export type Source = Schemas['SourceOut'];
export type Topic = Schemas['TopicOut'];
export type SourcesResponse = Schemas['SourcesResponse'];
export type MeResponse = Schemas['MeResponse'];
export type User = Schemas['UserOut'];
export type Preferences = Schemas['PreferencesOut'];
export type PreferencesPatch = Schemas['PreferencesPatch'];
export type ReadState = Schemas['ReadStateOut'];
/** Values of the feed's `read_state` query parameter — `all` is the default. */
export type ReadFilter = Schemas['ReadFilter'];
export type Theme = Schemas['Theme'];
export type Layout = Schemas['Layout'];
export type ApiToken = Schemas['ApiTokenOut'];
export type ApiTokenList = Schemas['ApiTokenList'];
export type ApiTokenCreate = Schemas['ApiTokenCreate'];
/** The only response that ever carries a raw token; `value` is shown once. */
export type ApiTokenCreated = Schemas['ApiTokenCreated'];
export type ApiTokenScope = Schemas['ApiTokenScope'];
