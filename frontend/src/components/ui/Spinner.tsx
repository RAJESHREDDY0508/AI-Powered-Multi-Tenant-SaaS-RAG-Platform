import { clsx } from 'clsx';

interface SpinnerProps {
  size?:      'sm' | 'md' | 'lg';
  className?: string;
  label?:     string;
}

const SIZE: Record<string, string> = {
  sm: 'h-4 w-4 border-2',
  md: 'h-6 w-6 border-2',
  lg: 'h-8 w-8 border-2',
};

export function Spinner({ size = 'md', className, label = 'Loading…' }: SpinnerProps) {
  return (
    <span
      role="status"
      aria-label={label}
      className={clsx(
        'inline-block rounded-full border-current border-r-transparent animate-spin',
        SIZE[size],
        className,
      )}
    />
  );
}

export function PageSpinner() {
  return (
    <div className="flex items-center justify-center min-h-[400px]">
      <Spinner size="lg" className="text-brand-500" />
    </div>
  );
}
