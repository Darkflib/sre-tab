import { useContext } from 'react';

import { CatalogueContext, type CatalogueValue } from './CatalogueProvider';

export function useCatalogue(): CatalogueValue {
  const value = useContext(CatalogueContext);
  if (!value) throw new Error('useCatalogue must be used inside <CatalogueProvider>');
  return value;
}
