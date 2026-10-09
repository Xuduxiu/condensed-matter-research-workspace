type Props = {
  label: string;
  value: string | number;
  detail?: string;
};

export default function StatCard({ label, value, detail }: Props) {
  return (
    <section className="stat-card">
      <span>{label}</span>
      <strong>{value}</strong>
      {detail && <small>{detail}</small>}
    </section>
  );
}
