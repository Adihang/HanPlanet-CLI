import React, {useEffect, useState} from 'react';
import {Box, Text} from 'ink';

export function OAuthCountdown({
	message,
	endsAt,
}: {
	message: string;
	endsAt: number;
}): React.JSX.Element {
	const [remaining, setRemaining] = useState(() => Math.max(0, Math.round((endsAt - Date.now()) / 1000)));

	useEffect(() => {
		const tick = (): void => {
			const secs = Math.max(0, Math.round((endsAt - Date.now()) / 1000));
			setRemaining(secs);
		};
		tick();
		const id = setInterval(tick, 1000);
		return () => clearInterval(id);
	}, [endsAt]);

	const mins = Math.floor(remaining / 60);
	const secs = remaining % 60;
	const timer = `${mins}:${String(secs).padStart(2, '0')}`;

	return (
		<Box>
			<Text color="cyan"> ℹ </Text>
			<Text>{message}</Text>
			<Text dimColor>  {timer}</Text>
		</Box>
	);
}
