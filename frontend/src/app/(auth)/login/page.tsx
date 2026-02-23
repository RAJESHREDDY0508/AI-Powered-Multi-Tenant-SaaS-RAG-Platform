'use client';

import { useState, useEffect } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { useForm } from 'react-hook-form';
import { Eye, EyeOff, Mail, Lock, Cpu } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { Input }  from '@/components/ui/Input';
import { useAuth } from '@/hooks/useAuth';
import { useToast } from '@/hooks/useToast';
import { ROUTES } from '@/lib/constants';

interface LoginFormData {
  email:    string;
  password: string;
  remember: boolean;
}

export default function LoginPage() {
  const { login }   = useAuth();
  const { error: toastError } = useToast();
  const searchParams = useSearchParams();
  const next         = searchParams.get('next') ?? ROUTES.DASHBOARD;

  const [showPassword, setShowPassword] = useState(false);
  const [isLoading,    setIsLoading]    = useState(false);

  const {
    register,
    handleSubmit,
    setError,
    formState: { errors },
  } = useForm<LoginFormData>({ defaultValues: { remember: false } });

  const onSubmit = async (data: LoginFormData) => {
    setIsLoading(true);
    try {
      await login({ email: data.email, password: data.password }, next);
    } catch (err: any) {
      const msg = err?.message ?? 'Invalid email or password';
      setError('root', { message: msg });
      toastError(msg);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="w-full max-w-md animate-fade-in">
      {/* Card */}
      <div className="bg-white dark:bg-gray-900 rounded-2xl shadow-xl border border-gray-200 dark:border-gray-800 p-8">
        {/* Logo */}
        <div className="flex justify-center mb-6">
          <div className="flex items-center gap-2">
            <Cpu className="h-8 w-8 text-brand-600" />
            <span className="text-xl font-bold text-gray-900 dark:text-gray-100">RAG Platform</span>
          </div>
        </div>

        <h1 className="text-2xl font-bold text-center text-gray-900 dark:text-gray-100 mb-1">
          Welcome back
        </h1>
        <p className="text-sm text-center text-gray-500 dark:text-gray-400 mb-8">
          Sign in to your workspace
        </p>

        <form onSubmit={handleSubmit(onSubmit)} noValidate className="space-y-4">
          <Input
            label="Email"
            type="email"
            autoComplete="email"
            placeholder="you@company.com"
            leftIcon={<Mail className="h-4 w-4" />}
            error={errors.email?.message}
            {...register('email', {
              required: 'Email is required',
              pattern:  { value: /^\S+@\S+\.\S+$/, message: 'Enter a valid email' },
            })}
          />

          <Input
            label="Password"
            type={showPassword ? 'text' : 'password'}
            autoComplete="current-password"
            placeholder="••••••••"
            leftIcon={<Lock className="h-4 w-4" />}
            rightIcon={
              <button
                type="button"
                onClick={() => setShowPassword((s) => !s)}
                className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
                aria-label={showPassword ? 'Hide password' : 'Show password'}
              >
                {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            }
            error={errors.password?.message}
            {...register('password', {
              required:  'Password is required',
              minLength: { value: 8, message: 'Password must be at least 8 characters' },
            })}
          />

          <div className="flex items-center justify-between text-sm">
            <label className="flex items-center gap-2 cursor-pointer text-gray-600 dark:text-gray-400">
              <input
                type="checkbox"
                className="rounded border-gray-300 text-brand-600 focus:ring-brand-500"
                {...register('remember')}
              />
              Remember me
            </label>
            <a href="#" className="text-brand-600 hover:text-brand-700 font-medium">
              Forgot password?
            </a>
          </div>

          {errors.root && (
            <p className="text-sm text-red-600 dark:text-red-400 text-center">
              {errors.root.message}
            </p>
          )}

          <Button type="submit" fullWidth loading={isLoading} size="md">
            Sign in
          </Button>
        </form>

        <p className="text-sm text-center text-gray-500 dark:text-gray-400 mt-6">
          Don&apos;t have an account?{' '}
          <Link href={ROUTES.REGISTER} className="text-brand-600 hover:text-brand-700 font-medium">
            Sign up
          </Link>
        </p>
      </div>
    </div>
  );
}
