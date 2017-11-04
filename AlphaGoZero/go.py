import numpy as np
from copy import deepcopy

WHITE = -1
BLACK = +1
EMPTY = 0
PASS_MOVE = None


class GameState(object):
	"""State of a game of Go and some basic functions to interact with it
	"""

	# Looking up positions adjacent to a given position takes a surprising
	# amount of time, hence this shared lookup table {boardsize: {position: [neighbors]}}
	__NEIGHBORS_CACHE = {}

	def __init__(self, size=19, komi=7.5, enforce_superko=False, history_length=8):
		self.board = np.zeros((size, size), dtype=int)
		self.board.fill(EMPTY)
		self.size = size
		self.current_player = BLACK
		self.ko = None
		self.komi = komi  # Komi is number of extra points WHITE gets for going 2nd
		self.handicaps = []
		self.history = []
		# Keeps 8 history board for fast feature extraction
		# Fill zeros for non-existing histories
		# board_history does not include the current board while the feature does,
		self.board_history = [np.zeros((size, size), dtype=int) for _ in range(history_length - 1)]
		self.num_black_prisoners = 0
		self.num_white_prisoners = 0
		self.is_end_of_game = False
		# Each pass move by a player subtracts a point
		self.passes_white = 0
		self.passes_black = 0
		# `self.liberty_sets` is a 2D array with the same indexes as `board`
		# each entry points to a set of tuples - the liberties of a stone's
		# connected block. By caching liberties in this way, we can directly
		# optimize update functions (e.g. do_move) and in doing so indirectly
		# speed up any function that queries liberties
		self._create_neighbors_cache()
		self.liberty_sets = [[set() for _ in range(size)] for _ in range(size)]
		for x in range(size):
			for y in range(size):
				self.liberty_sets[x][y] = set(self._neighbors((x, y)))
		# separately cache the 2D numpy array of the _size_ of liberty sets
		# at each board position
		self.liberty_counts = np.zeros((size, size), dtype=int)
		self.liberty_counts.fill(-1)
		# initialize liberty_sets of empty board: the set of neighbors of each position
		# similarly to `liberty_sets`, `group_sets[x][y]` points to a set of tuples
		# containing all (x',y') pairs in the group connected to (x,y)
		self.group_sets = [[set() for _ in range(size)] for _ in range(size)]
		# cache of list of legal moves (actually 'sensible' moves, with a
		# separate list for eye-moves on request)
		self.__legal_move_cache = None
		self.__legal_eyes_cache = None
		# on-the-fly record of 'age' of each stone
		self.stone_ages = np.zeros((size, size), dtype=np.int) - 1

		# setup Zobrist hash to keep track of board state
		self.enforce_superko = enforce_superko
		rng = np.random.RandomState(0)
		self.hash_lookup = {
			WHITE: rng.randint(np.iinfo(np.uint64).max, size=(size, size), dtype='uint64'),
			BLACK: rng.randint(np.iinfo(np.uint64).max, size=(size, size), dtype='uint64')}
		self.current_hash = np.uint64(0)
		self.previous_hashes = set()

	def get_group(self, position):
		"""Get the group of connected same-color stones to the given position
		Keyword arguments:
		position -- a tuple of (x, y)
		x being the column index of the starting position of the search
		y being the row index of the starting position of the search
		Return:
		a set of tuples consist of (x, y)s which are the same-color cluster
		which contains the input single position. len(group) is size of the cluster, can be large.
		"""
		(x, y) = position
		# given that this is already cached, it is a fast lookup
		return self.group_sets[x][y]

	def get_groups_around(self, position):
		"""returns a list of the unique groups adjacent to position
		'unique' means that, for example in this position:
			. . . . .
			. B W . .
			. W W . .
			. . . . .
			. . . . .
		only the one white group would be returned on get_groups_around((1,1))
		"""
		groups = []
		for (nx, ny) in self._neighbors(position):
			group = self.group_sets[nx][ny]
			if len(group) > 0 and group not in groups:
				groups.append(self.group_sets[nx][ny])
		return groups

	def _on_board(self, position):
		"""simply return True iff position is within the bounds of [0, self.size)
		"""
		(x, y) = position
		return x >= 0 and y >= 0 and x < self.size and y < self.size

	def _create_neighbors_cache(self):
		if self.size not in GameState.__NEIGHBORS_CACHE:
			GameState.__NEIGHBORS_CACHE[self.size] = {}
			for x in range(self.size):
				for y in range(self.size):
					neighbors = [xy for xy in [(x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)]
								 if self._on_board(xy)]
					GameState.__NEIGHBORS_CACHE[self.size][(x, y)] = neighbors

	def _neighbors(self, position):
		"""A private helper function that simply returns a list of positions neighboring
		the given (x,y) position. Basically it handles edges and corners.
		"""
		return GameState.__NEIGHBORS_CACHE[self.size][position]

	def _diagonals(self, position):
		"""Like _neighbors but for diagonal positions
		"""
		(x, y) = position
		return filter(self._on_board, [(x - 1, y - 1), (x + 1, y + 1),
									   (x + 1, y - 1), (x - 1, y + 1)])

	def _update_neighbors(self, position):
		"""A private helper function to update self.group_sets and self.liberty_sets
		given that a stone was just played at `position`
		"""
		(x, y) = position

		merged_group = set()
		merged_group.add(position)
		merged_libs = self.liberty_sets[x][y]
		for (nx, ny) in self._neighbors(position):
			# remove (x,y) from liberties of neighboring positions
			self.liberty_sets[nx][ny] -= set([position])
			# if neighbor was opponent, update group's liberties count
			# (current_player's groups will be updated below regardless)
			if self.board[nx][ny] == -self.current_player:
				new_liberty_count = len(self.liberty_sets[nx][ny])
				for (gx, gy) in self.group_sets[nx][ny]:
					self.liberty_counts[gx][gy] = new_liberty_count
			# MERGE group/liberty sets if neighbor is the same color
			# note: this automatically takes care of merging two separate
			# groups that just became connected through (x,y)
			elif self.board[x][y] == self.board[nx][ny]:
				merged_group |= self.group_sets[nx][ny]
				merged_libs |= self.liberty_sets[nx][ny]

		# now that we have one big 'merged' set for groups and liberties, loop
		# over every member of the same-color group to update them
		# Note: neighboring opponent groups are already updated in the previous loop
		count_merged_libs = len(merged_libs)
		for (gx, gy) in merged_group:
			self.group_sets[gx][gy] = merged_group
			self.liberty_sets[gx][gy] = merged_libs
			self.liberty_counts[gx][gy] = count_merged_libs

	def _update_hash(self, action, color):
		(x, y) = action
		self.current_hash = np.bitwise_xor(self.current_hash, self.hash_lookup[color][x][y])

	def _remove_group(self, group):
		"""A private helper function to take a group off the board (due to capture),
		updating group sets and liberties along the way
		"""
		for (x, y) in group:
			self._update_hash((x, y), self.board[x, y])
			self.board[x, y] = EMPTY
		for (x, y) in group:
			# clear group_sets for all positions in 'group'
			self.group_sets[x][y] = set()
			self.liberty_sets[x][y] = set()
			self.liberty_counts[x][y] = -1
			self.stone_ages[x][y] = -1
			for (nx, ny) in self._neighbors((x, y)):
				if self.board[nx, ny] == EMPTY:
					# add empty neighbors of (x,y) to its liberties
					self.liberty_sets[x][y].add((nx, ny))
				else:
					# add (x,y) to the liberties of its nonempty neighbors
					self.liberty_sets[nx][ny].add((x, y))
					for (gx, gy) in self.group_sets[nx][ny]:
						self.liberty_counts[gx][gy] = len(self.liberty_sets[nx][ny])

	def copy(self):
		"""get a copy of this Game state
		"""
		other = GameState(self.size, self.komi)
		other.board = self.board.copy()
		other.current_player = self.current_player
		other.ko = self.ko
		other.handicaps = list(self.handicaps)
		other.history = list(self.history)
		other.board_history = deepcopy(self.board_history)
		other.num_black_prisoners = self.num_black_prisoners
		other.num_white_prisoners = self.num_white_prisoners
		other.enforce_superko = self.enforce_superko
		other.current_hash = self.current_hash.copy()
		other.previous_hashes = self.previous_hashes.copy()

		# update liberty and group sets.
		#
		# group_sets and liberty_sets are shared between stones in the same
		# group.  We need to make sure this is the case in the copy, as well.
		#
		# we store set copies indexed by original id() in set_copies
		def get_copy(s, set_copies={}):
			if id(s) not in set_copies:
				set_copies[id(s)] = set(s)  # makes a copy of s
			return set_copies[id(s)]

		for x in range(self.size):
			for y in range(self.size):
				other.group_sets[x][y] = get_copy(self.group_sets[x][y])
				other.liberty_sets[x][y] = get_copy(self.liberty_sets[x][y])
		other.liberty_counts = self.liberty_counts.copy()
		return other

	def is_suicide(self, action):
		"""return true if having current_player play at <action> would be suicide
		"""
		(x, y) = action
		num_liberties_here = len(self.liberty_sets[x][y])
		if num_liberties_here == 0:
			# no liberties here 'immediately'
			# but this may still connect to another group of the same color
			for (nx, ny) in self._neighbors(action):
				# check if we're saved by attaching to a friendly group that has
				# liberties elsewhere
				is_friendly_group = self.board[nx, ny] == self.current_player
				group_has_other_liberties = len(self.liberty_sets[nx][ny] - set([action])) > 0
				if is_friendly_group and group_has_other_liberties:
					return False
				# check if we're killing an unfriendly group
				is_enemy_group = self.board[nx, ny] == -self.current_player
				if is_enemy_group and (not group_has_other_liberties):
					return False
			# checked all the neighbors, and it doesn't look good.
			return True
		return False

	def is_positional_superko(self, action):
		"""Find all actions that the current_player has done in the past, taking into
		account the fact that history starts with BLACK when there are no
		handicaps or with WHITE when there are.

		"""
		if len(self.handicaps) == 0 and self.current_player == BLACK:
			player_history = self.history[0::2]
		elif len(self.handicaps) > 0 and self.current_player == WHITE:
			player_history = self.history[0::2]
		else:
			player_history = self.history[1::2]

		if action not in self.handicaps and action not in player_history:
			return False

		state_copy = self.copy()
		state_copy.enforce_superko = False
		state_copy.do_move(action)

		if state_copy.current_hash in self.previous_hashes:
			return True
		else:
			return False

	def is_legal(self, action):
		"""determine if the given action (x,y tuple) is a legal move
		note: we only check ko, not superko at this point (TODO?)
		"""
		# passing is always legal
		if action is PASS_MOVE:
			return True
		(x, y) = action
		if not self._on_board(action):
			return False
		if self.board[x][y] != EMPTY:
			return False
		if self.is_suicide(action):
			return False
		if action == self.ko:
			return False
		if self.enforce_superko and self.is_positional_superko(action):
			return False
		return True

	def is_eyeish(self, position, owner):
		"""returns whether the position is empty and is surrounded by all stones of 'owner'
		"""
		(x, y) = position
		if self.board[x, y] != EMPTY:
			return False

		for (nx, ny) in self._neighbors(position):
			if self.board[nx, ny] != owner:
				return False
		return True

	def is_eye(self, position, owner, stack=[]):
		"""returns whether the position is a true eye of 'owner'
		Requires a recursive call; empty spaces diagonal to 'position' are fine
		as long as they themselves are eyes
		"""
		if not self.is_eyeish(position, owner):
			return False
		# (as in Fuego/Michi/etc) ensure that num "bad" diagonals is 0 (edges) or 1
		# where a bad diagonal is an opponent stone or an empty non-eye space
		num_bad_diagonal = 0
		# if in middle of board, 1 bad neighbor is allowable; zero for edges and corners
		allowable_bad_diagonal = 1 if len(self._neighbors(position)) == 4 else 0

		for d in self._diagonals(position):
			# opponent stones count against this being eye
			if self.board[d] == -owner:
				num_bad_diagonal += 1
			# empty spaces (that aren't themselves eyes) count against it too
			# the 'stack' keeps track of where we've already been to prevent
			# infinite loops of recursion
			elif self.board[d] == EMPTY and d not in stack:
				stack.append(position)
				if not self.is_eye(d, owner, stack):
					num_bad_diagonal += 1
				stack.pop()
			# at any point, if we've surpassed # allowable, we can stop
			if num_bad_diagonal > allowable_bad_diagonal:
				return False
		return True

	def get_legal_moves(self, include_eyes=True):
		if self.__legal_move_cache is not None:
			if include_eyes:
				return self.__legal_move_cache + self.__legal_eyes_cache
			else:
				return self.__legal_move_cache
		self.__legal_move_cache = []
		self.__legal_eyes_cache = []
		for x in range(self.size):
			for y in range(self.size):
				if self.is_legal((x, y)):
					if not self.is_eye((x, y), self.current_player):
						self.__legal_move_cache.append((x, y))
					else:
						self.__legal_eyes_cache.append((x, y))
		return self.get_legal_moves(include_eyes)

	def get_winner(self):
		"""Calculate score of board state and return player ID (1, -1, or 0 for tie)
		corresponding to winner. Uses 'Area scoring'.
		"""
		# Count number of positions filled by each player, plus 1 for each eye-ish space owned
		score_white = np.sum(self.board == WHITE)
		score_black = np.sum(self.board == BLACK)
		empties = zip(*np.where(self.board == EMPTY))
		for empty in empties:
			# Check that all surrounding points are of one color
			if self.is_eyeish(empty, BLACK):
				score_black += 1
			elif self.is_eyeish(empty, WHITE):
				score_white += 1
		score_white += self.komi
		score_white -= self.passes_white
		score_black -= self.passes_black
		if score_black > score_white:
			winner = BLACK
		elif score_white > score_black:
			winner = WHITE
		else:
			# Tie
			winner = 0
		return winner

	def place_handicaps(self, actions):
		if len(self.history) > 0:
			raise IllegalMove("Cannot place handicap on a started game")
		self.handicaps.extend(actions)
		for action in actions:
			self.do_move(action, BLACK)
		self.history = []
		self.board_history = [np.zeros((size, size), dtype=int) for _ in range(history_length - 1)]

	def get_current_player(self):
		"""Returns the color of the player who will make the next move.
		"""
		return self.current_player

	def do_move(self, action, color=None):
		"""Play stone at action=(x,y). If color is not specified, current_player is used
		If it is a legal move, current_player switches to the opposite color
		If not, an IllegalMove exception is raised
		"""
		color = color or self.current_player
		reset_player = self.current_player
		self.current_player = color
		if self.is_legal(action):
			# reset ko
			self.ko = None
			# increment age of stones by 1
			self.stone_ages[self.stone_ages >= 0] += 1
			# save current board to history before it is modified
			self.board_history.append(self.board.copy())
			self.board_history.pop(0)

			if action is not PASS_MOVE:
				(x, y) = action
				self.board[x][y] = color
				self._update_hash(action, color)
				self._update_neighbors(action)
				self.stone_ages[x][y] = 0

				# check neighboring groups' liberties for captures
				for (nx, ny) in self._neighbors(action):
					if self.board[nx, ny] == -color and len(self.liberty_sets[nx][ny]) == 0:
						# capture occurred!
						captured_group = self.group_sets[nx][ny]
						num_captured = len(captured_group)
						self._remove_group(captured_group)
						if color == BLACK:
							self.num_white_prisoners += num_captured
						else:
							self.num_black_prisoners += num_captured
						# check for ko
						if num_captured == 1:
							# it is a ko iff, were the opponent to play at the captured position,
							# it would recapture (x,y) only
							# (a bigger group containing xy may be captured - this is 'snapback')
							would_recapture = len(self.liberty_sets[x][y]) == 1
							recapture_size_is_1 = len(self.group_sets[x][y]) == 1
							if would_recapture and recapture_size_is_1:
								# note: (nx,ny) is the stone that was captured
								self.ko = (nx, ny)
				# _remove_group has finished updating the hash
				self.previous_hashes.add(self.current_hash)
			else:
				if color == BLACK:
					self.passes_black += 1
				if color == WHITE:
					self.passes_white += 1
			# next turn
			self.current_player = -color
			self.history.append(action)
			self.__legal_move_cache = None
		else:
			self.current_player = reset_player
			raise IllegalMove(str(action))
		# Check for end of game
		if len(self.history) > 1:
			if self.history[-1] is PASS_MOVE and self.history[-2] is PASS_MOVE \
					and self.current_player == WHITE:
				self.is_end_of_game = True
		return self.is_end_of_game

	def transform(transform_id):
		""" Transform the current board and the history boards according to D(4).
			Caution: self.history (action history) is not modified, thus this function
			should ONLY be used for state evaluation.
			Arguments:
				transform_id: integer in range [0, 7]
		"""
		# List of boards to transform
		boards_to_transform = self.board_history + [self.board]
		for b in boards_to_transform:
			# Performs reflection
			if transform_id // 4 == 1:
				b = np.fliplr(b)
			# Performs rotation
			b = np.rot90(b, transform_id % 4)




class IllegalMove(Exception):
	pass
