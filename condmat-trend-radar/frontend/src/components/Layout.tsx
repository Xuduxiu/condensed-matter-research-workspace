import { ReactNode } from 'react';
import { PageKey } from '../App';
import { Language, Translation } from '../i18n';
import Sidebar from './Sidebar';

type Props = {
  children: ReactNode;
  page: PageKey;
  language: Language;
  t: Translation;
  onPageChange: (page: PageKey) => void;
  onLanguageChange: (language: Language) => void;
};

export default function Layout({ children, page, language, t, onPageChange, onLanguageChange }: Props) {
  return (
    <div className="app-shell">
      <Sidebar active={page} language={language} t={t} onPageChange={onPageChange} onLanguageChange={onLanguageChange} />
      <main className="content-shell">{children}</main>
    </div>
  );
}
