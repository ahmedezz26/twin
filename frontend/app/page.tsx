import Twin from '@/components/twin';

export default function Home() {
  return (
    <main className="min-h-screen bg-gradient-to-br from-slate-50 to-gray-100">
      <div className="container mx-auto px-4 py-8">
        <div className="max-w-4xl mx-auto">
          <h1 className="text-4xl font-bold text-center text-gray-800 mb-2">
            Welcome to my digital twin
          </h1>
          <p className="text-center text-gray-600 mb-8">
            Feel free to ask about my professional career
          </p>

          <div className="h-[600px]">
            <Twin />
          </div>
        </div>
      </div>
    </main>
  );
}
