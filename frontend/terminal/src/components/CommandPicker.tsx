import React from 'react';
import {Box, Text} from 'ink';

import type {CommandInfo} from '../types.js';

function CommandPickerInner({
	hints,
	selectedIndex,
}: {
	hints: CommandInfo[];
	selectedIndex: number;
}): React.JSX.Element | null {
	if (hints.length === 0) {
		return null;
	}

	return (
		<Box flexDirection="column" borderStyle="round" borderColor="cyan" paddingX={1} marginBottom={0}>
			<Text dimColor bold> Commands</Text>
			{hints.map((hint, i) => {
				const isSelected = i === selectedIndex;
				return (
					<Box key={hint.name} justifyContent="space-between">
						<Box>
							<Text color={isSelected ? 'cyan' : undefined} bold={isSelected}>
								{isSelected ? '\u276F ' : '  '}
								{hint.name}
							</Text>
						</Box>
						<Box>
							<Text dimColor>  {hint.description}</Text>
						</Box>
					</Box>
				);
			})}
			<Text dimColor> {'\u2191\u2193'} navigate{'  '}{'\u23CE'} select{'  '}esc dismiss</Text>
		</Box>
	);
}

export const CommandPicker = React.memo(CommandPickerInner);
